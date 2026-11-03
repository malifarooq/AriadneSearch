"""Read models shared by the web server and the CLI."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from typing import Literal

from ariadnesearch.db import chunks, usage_today, utc_now
from ariadnesearch.errors import NotFound
from ariadnesearch.journals import JournalImpact, impact_for_works
from ariadnesearch.mine import MyFlags, annotate
from ariadnesearch.rank import (
    KNOWN_STATES,
    Scored,
    explain,
    load_snapshot,
    load_weights,
    rank,
    select_frontier,
)
from ariadnesearch.venues import TARGET_EDITION, VenueRank, meets_minimum, ranks_for_works

SHOWN_EVIDENCE = 5
NOT_POPULATED = "Its neighbours are not added yet. Press Update graph."
SEARCH_AUTHORS = 3  # authors sent with each graph node and search result
SEARCH_WORDS = 8
_STATE_ORDER = {"seed": 0, "accepted": 0, "read": 1, "frontier": 2, "rejected": 3}

GraphMode = Literal["all", "added", "recent"]
SortOrder = Literal["relevance", "impact", "citations", "newest"]


def work_summaries(
    conn: sqlite3.Connection, work_ids: list[str]
) -> dict[str, dict]:
    """Return display fields for papers."""
    summaries: dict[str, dict] = {}
    for chunk in chunks(work_ids, 500):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM work WHERE id IN ({marks})", chunk):
            summaries[row["id"]] = {
                "id": row["id"],
                "title": row["title"],
                "authors": json.loads(row["authors"] or "[]"),
                "year": row["year"],
                "pub_date": row["pub_date"],
                "topic": row["topic"] or None,
                "subfield": row["subfield"],
                "venue": row["venue"],
                "venue_type": row["venue_type"],
                "cited_by": row["oa_cited_by"],
                "percentile": row["oa_pct"],
                "n_refs": row["n_refs"],
                "level": row["level"],
            }
        rows = conn.execute(
            f"SELECT work_id, scheme, value FROM work_alias WHERE work_id IN ({marks})",
            chunk,
        )
        for row in rows:
            if row["scheme"] in ("doi", "arxiv"):
                summary = summaries.get(row["work_id"])
                if summary is None:
                    continue
                # arXiv papers also have a 10.48550 DOI. Prefer any other DOI.
                if row["scheme"] == "doi" and summary.get("doi") and row[
                    "value"
                ].startswith("10.48550/"):
                    continue
                summary[row["scheme"]] = row["value"]
    return summaries


def _short_label(summary: dict) -> str:
    authors = summary.get("authors") or []
    name = authors[0].split()[-1] if authors and authors[0].split() else "Unknown"
    return f"{name} {summary.get('year') or 'n.d.'}"


def _titles(summaries: dict[str, dict], work_ids: list[str]) -> list[dict]:
    return [
        {"id": work_id, "title": summaries[work_id].get("title") or "Untitled"}
        for work_id in work_ids[:SHOWN_EVIDENCE]
        if work_id in summaries
    ]


def _reasons(
    evidence: dict[str, list[str]],
    terms: dict[str, float] | None = None,
    venue_rank: VenueRank | None = None,
) -> list[str]:
    """Describe in plain language why a paper was suggested."""
    reasons = []
    counts = (
        ("cites_known", "Cites {} in this project", "paper"),
        ("cited_by_known", "Cited by {} in this project", "paper"),
        ("shared_refs", "Shares {} with this project's papers", "reference"),
        ("shared_citers", "Cited alongside this project's papers by {}", "paper"),
    )
    for key, template, noun in counts:
        count = len(evidence.get(key, []))
        if count:
            reasons.append(template.format(f"{count} {noun}{'' if count == 1 else 's'}"))
    if venue_rank and (terms or {}).get("venue"):
        direction = "raises" if terms["venue"] > 0 else "lowers"
        source = "Target" if venue_rank.edition == TARGET_EDITION else "CORE"
        reasons.append(f"{source} {venue_rank.rank} venue, which {direction} its score")
    return reasons


def _rank_label(rank_value: VenueRank | None) -> dict | None:
    return asdict(rank_value) if rank_value else None


def _impact_label(impact: JournalImpact | None) -> dict | None:
    return asdict(impact) if impact else None


def frontier_view(
    conn: sqlite3.Connection,
    project_id: int,
    k: int = 50,
    hide_cited: bool = False,
    hide_library: bool = False,
    min_rank: str | None = None,
    min_year: int | None = None,
    min_impact: float | None = None,
    sort: SortOrder = "relevance",
) -> dict:
    """Return ranked suggestions after applying filters.

    `min_year` keeps recent papers only, for the state-of-the-art list.
    `min_impact` keeps journal papers whose journal's 2-year impact reaches it.
    A `sort` other than relevance orders the matches by that instead and skips
    the exploration picks.
    """
    snapshot, state = load_snapshot(conn, project_id)
    weights = load_weights(conn, project_id)
    results = rank(snapshot, state, weights)
    ids = [item.work_id for item in results]
    flags = annotate(conn, ids)
    ranks = ranks_for_works(conn, ids)
    impact = impact_for_works(conn, ids)

    def journal_impact(work_id: int) -> float:
        found = impact.get(work_id)
        return found.mean_citedness or 0.0 if found else -1.0  # -1: not a journal

    def keep(item: Scored) -> bool:
        flag = flags[item.work_id]
        return (
            not (hide_cited and flag.cited_by_me)
            and not (hide_library and flag.in_library)
            and meets_minimum(ranks.get(item.work_id), min_rank)
            and (min_year is None or (snapshot.year.get(item.work_id) or 0) >= min_year)
            and (min_impact is None or journal_impact(item.work_id) >= min_impact)
        )

    matching = [item for item in results if keep(item)]
    if sort == "relevance":
        chosen = select_frontier(
            matching,
            k,
            weights.explore_fraction,
            seed=len(state.accepted) + len(state.rejected),
        )
    else:
        orders = {
            "impact": lambda item: -journal_impact(item.work_id),
            "citations": lambda item: -(snapshot.cited_by.get(item.work_id) or 0),
            "newest": lambda item: -(snapshot.year.get(item.work_id) or 0),
        }
        chosen = sorted(matching, key=lambda item: (orders[sort](item), -item.score))[:k]
    explained = explain(snapshot, state, [item.work_id for item in chosen])
    for item in chosen:
        item.evidence = explained[item.work_id]

    needed = {item.work_id for item in chosen}
    for item in chosen:
        for key in ("cites_known", "cited_by_known"):
            needed.update(item.evidence.get(key, [])[:SHOWN_EVIDENCE])
    summaries = work_summaries(conn, sorted(needed))

    items = []
    for item in chosen:
        summary = summaries.get(item.work_id)
        if summary is None:
            continue
        items.append(
            {
                **summary,
                "label": _short_label(summary),
                "score": round(item.score, 4),
                "terms": {key: round(value, 3) for key, value in item.terms.items()},
                "reasons": _reasons(item.evidence, item.terms, ranks.get(item.work_id)),
                "linked": {
                    "cites": _titles(summaries, item.evidence.get("cites_known", [])),
                    "cited_by": _titles(
                        summaries, item.evidence.get("cited_by_known", [])
                    ),
                },
                "explore": item.explore,
                "mine": asdict(flags[item.work_id]),
                "rank": _rank_label(ranks.get(item.work_id)),
                "journal": _impact_label(impact.get(item.work_id)),
            }
        )
    return {"items": items, "total": len(state.frontier), "matching": len(matching)}


def _like(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_view(
    conn: sqlite3.Connection, project_id: int, query: str, limit: int = 20
) -> dict:
    """Find papers in a project by title, author, year or DOI.

    Every word must match one of those. Your own papers come first, then
    suggestions, then rejected papers, and titles starting with the query lead.
    """
    words = query.lower().split()[:SEARCH_WORDS]
    if not words:
        return {"items": []}
    clauses: list[str] = []
    params: list = [project_id]
    for word in words:
        like = _like(word)
        clauses.append(
            "(LOWER(w.title) LIKE ? ESCAPE '\\' OR LOWER(w.authors) LIKE ? ESCAPE '\\'"
            " OR CAST(w.year AS TEXT) = ? OR EXISTS (SELECT 1 FROM work_alias a"
            " WHERE a.work_id = w.id AND a.scheme = 'doi' AND a.value LIKE ? ESCAPE '\\'))"
        )
        params += [like, like, word, like]
    rows = conn.execute(
        f"""
        SELECT w.id, w.title, w.year, w.authors, m.state
        FROM membership m JOIN work w ON w.id = m.work_id
        WHERE m.project_id = ? AND {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    phrase = query.strip().lower()
    rows = sorted(
        rows,
        key=lambda row: (
            _STATE_ORDER.get(row["state"], 4),
            not (row["title"] or "").lower().startswith(phrase),
            -(row["year"] or 0),
            row["id"],
        ),
    )[:limit]
    items = []
    for row in rows:
        authors = json.loads(row["authors"] or "[]")
        summary = {"authors": authors, "year": row["year"]}
        items.append(
            {
                "id": row["id"],
                "title": row["title"] or "Untitled",
                "year": row["year"],
                "state": row["state"],
                "label": _short_label(summary),
                "authors": authors[:SEARCH_AUTHORS],
            }
        )
    return {"items": items}


def authors_view(conn: sqlite3.Connection, project_id: int, limit: int = 40) -> dict:
    """Return the project's most frequent authors and their papers.

    Authors of your added papers come first, then by how many suggestions they
    wrote. Names are matched as written, so one person may appear twice.
    """
    rows = conn.execute(
        """
        SELECT m.work_id, m.state, w.authors, w.year, w.title FROM membership m
        JOIN work w ON w.id = m.work_id
        WHERE m.project_id = ? AND m.state != 'rejected'
        """,
        (project_id,),
    ).fetchall()
    authors: dict[str, dict] = {}
    for row in sorted(rows, key=lambda row: -(row["year"] or 0)):
        names = json.loads(row["authors"] or "[]")
        label = _short_label({"authors": names, "year": row["year"]})
        for name in names:
            entry = authors.setdefault(
                name, {"name": name, "added": 0, "suggested": 0, "work_ids": [], "papers": []}
            )
            entry["added" if row["state"] in KNOWN_STATES else "suggested"] += 1
            entry["work_ids"].append(row["work_id"])
            entry["papers"].append(
                {"id": row["work_id"], "label": label, "title": row["title"] or "Untitled"}
            )
    ranked = sorted(
        authors.values(),
        key=lambda entry: (-entry["added"], -len(entry["work_ids"]), entry["name"]),
    )
    return {"items": [entry for entry in ranked if len(entry["work_ids"]) > 1][:limit]}


def added_view(conn: sqlite3.Connection, project_id: int) -> dict:
    """Return the papers you added, in the same shape as suggestions."""
    rows = conn.execute(
        "SELECT m.work_id, m.state, p.work_id IS NOT NULL AS populated "
        "FROM membership m LEFT JOIN populated p "
        "ON p.project_id = m.project_id AND p.work_id = m.work_id "
        "WHERE m.project_id = ? AND m.state IN ('seed', 'accepted', 'read')",
        (project_id,),
    ).fetchall()
    ids = [row["work_id"] for row in rows]
    summaries = work_summaries(conn, ids)
    flags = annotate(conn, ids)
    ranks = ranks_for_works(conn, ids)
    impact = impact_for_works(conn, ids)
    order = {"seed": 0, "accepted": 1, "read": 2}
    items = []
    for row in sorted(
        rows,
        key=lambda row: (
            order[row["state"]],
            -(summaries.get(row["work_id"], {}).get("year") or 0),
            row["work_id"],
        ),
    ):
        summary = summaries.get(row["work_id"])
        if summary is None:
            continue
        items.append(
            {
                **summary,
                "label": _short_label(summary),
                "state": row["state"],
                "populated": bool(row["populated"]),
                "reasons": [] if row["populated"] else [NOT_POPULATED],
                "explore": False,
                "mine": asdict(flags[row["work_id"]]),
                "rank": _rank_label(ranks.get(row["work_id"])),
                "journal": _impact_label(impact.get(row["work_id"])),
            }
        )
    return {"items": items, "total": len(items)}


def graph_view(
    conn: sqlite3.Connection,
    project_id: int,
    limit: int = 200,
    mode: GraphMode = "all",
    min_year: int | None = None,
    include: list[int] | None = None,
) -> dict:
    """Return graph nodes and edges, with saved positions and importance fields.

    `include` lists project papers to draw whatever their rank, such as citing
    papers just added for review.

    Modes: "all" shows added papers and the best suggestions, "added" shows only
    seeds, accepted and read papers, and "recent" shows papers from `min_year` on.
    Each node carries the figures the page can size it by: citations, the field-
    and year-normalized citation percentile, its relevance score, and how many
    papers in this project cite it.
    """
    members = {
        row["work_id"]: dict(row)
        for row in conn.execute(
            "SELECT work_id, state, x, y FROM membership WHERE project_id = ?",
            (project_id,),
        )
    }
    known = [
        work_id
        for work_id, row in members.items()
        if row["state"] in ("seed", "accepted", "read")
    ]
    snapshot, state = load_snapshot(conn, project_id)
    scores: dict[str, float] = {}
    if mode != "added":
        scores = {
            item.work_id: item.score
            for item in rank(snapshot, state, load_weights(conn, project_id))
        }
    if mode == "recent":
        def recent(work_id: int) -> bool:
            return (snapshot.year.get(work_id) or 0) >= (min_year or 0)

        known = [work_id for work_id in known if recent(work_id)]
        suggested = [work_id for work_id in scores if recent(work_id)]
    else:
        suggested = list(scores)
    known = known[:limit]
    visible = known + suggested[: max(limit - len(known), 0)]
    shown = set(visible)
    visible += [work_id for work_id in include or [] if work_id in members and work_id not in shown]

    summaries = work_summaries(conn, visible)
    flags = annotate(conn, visible)
    ranks = ranks_for_works(conn, visible)
    cited_in_project = _cited_in_project(conn, project_id, visible)
    nodes = []
    for work_id in visible:
        summary = summaries.get(work_id)
        if summary is None:
            continue
        member = members[work_id]
        flag: MyFlags = flags[work_id]
        nodes.append(
            {
                "id": work_id,
                "label": _short_label(summary),
                "title": summary["title"] or "Untitled",
                "year": summary["year"],
                "pub_date": summary["pub_date"],
                "authors": summary["authors"][:SEARCH_AUTHORS],
                "topic": summary["topic"],
                "subfield": summary["subfield"],
                "cited_by": summary["cited_by"] or 0,
                "percentile": summary["percentile"],
                "score": round(scores[work_id], 4) if work_id in scores else None,
                "project_citations": cited_in_project.get(work_id, 0),
                "state": member["state"],
                "cited_by_me": flag.cited_by_me,
                "in_library": flag.in_library,
                "rank": ranks[work_id].rank if work_id in ranks else None,
                "x": member["x"],
                "y": member["y"],
            }
        )

    shown = {node["id"] for node in nodes}
    edges = []
    for chunk in chunks(sorted(shown), 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT src, dst FROM cites WHERE src IN ({marks})", chunk
        )
        edges.extend(
            {"source": row["src"], "target": row["dst"]}
            for row in rows
            if row["dst"] in shown
        )
    return {"nodes": nodes, "edges": edges, "total": len(members)}


def _cited_in_project(
    conn: sqlite3.Connection, project_id: int, work_ids: list[str]
) -> dict[str, int]:
    """Count how many papers in the project cite each of these papers."""
    counts: dict[str, int] = {}
    for chunk in chunks(work_ids, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT c.dst, COUNT(*) AS n FROM cites c
            JOIN membership m ON m.work_id = c.src AND m.project_id = ?
            WHERE c.dst IN ({marks}) AND m.state != 'rejected'
            GROUP BY c.dst
            """,
            (project_id, *chunk),
        )
        counts.update({row["dst"]: row["n"] for row in rows})
    return counts


def save_positions(
    conn: sqlite3.Connection, project_id: int, positions: list[dict]
) -> None:
    """Store node positions, so the map stays stable between sessions."""
    conn.executemany(
        "UPDATE membership SET x = ?, y = ? WHERE project_id = ? AND work_id = ?",
        [(item["x"], item["y"], project_id, item["id"]) for item in positions],
    )
    conn.commit()


def work_view(conn: sqlite3.Connection, project_id: int, work_id: int) -> dict:
    """Return full details for one paper."""
    row = conn.execute("SELECT * FROM work WHERE id = ?", (work_id,)).fetchone()
    if row is None:
        raise NotFound(f"Unknown paper: {work_id}")
    summary = work_summaries(conn, [work_id])[work_id]
    member = conn.execute(
        "SELECT state FROM membership WHERE project_id = ? AND work_id = ?",
        (project_id, work_id),
    ).fetchone()
    ranks = ranks_for_works(conn, [work_id])
    related = conn.execute(
        """
        SELECT w.id, w.title, r.kind FROM relation r
        JOIN work w ON w.id = CASE WHEN r.a = ? THEN r.b ELSE r.a END
        WHERE r.a = ? OR r.b = ?
        """,
        (work_id, work_id, work_id),
    ).fetchall()
    capped = conn.execute(
        "SELECT citers_total FROM fetch_state WHERE work_id = ?", (work_id,)
    ).fetchone()
    return {
        **summary,
        "abstract": row["abstract"],
        "state": member["state"] if member else None,
        "mine": asdict(annotate(conn, [work_id])[work_id]),
        "rank": _rank_label(ranks.get(work_id)),
        "journal": _impact_label(impact_for_works(conn, [work_id]).get(work_id)),
        "citers_total": capped["citers_total"] if capped else None,
        "related": [dict(item) for item in related],
    }


def project_summary(conn: sqlite3.Connection, project_id: int) -> dict:
    """Return paper counts by state."""
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM membership WHERE project_id = ? GROUP BY state",
        (project_id,),
    )
    return {row["state"]: row["n"] for row in rows}


def usage_view(conn: sqlite3.Connection) -> dict:
    """Return today's API use."""
    openalex_calls, openalex_cost = usage_today(conn, "openalex")
    ieee_calls, _ = usage_today(conn, "ieee")
    return {
        "openalex_calls": openalex_calls,
        "openalex_cost_usd": round(openalex_cost, 4),
        "ieee_calls": ieee_calls,
        "day": utc_now()[:10],
    }


def _bib_escape(text: str) -> str:
    return re.sub(r"([&%$#_])", r"\\\1", text)


def _bib_key(summary: dict, used: set[str]) -> str:
    name = _short_label(summary).replace(" ", "")
    words = re.findall(r"[A-Za-z]{4,}", summary.get("title") or "")
    base = re.sub(r"[^A-Za-z0-9]", "", name + (words[0].lower() if words else ""))
    key, suffix = base, 1
    while key in used:
        suffix += 1
        key = f"{base}{suffix}"
    used.add(key)
    return key


def export_bib(conn: sqlite3.Connection, project_id: int) -> str:
    """Export seeds, accepted and read papers. This never includes IEEE fields."""
    rows = conn.execute(
        "SELECT work_id FROM membership WHERE project_id = ? "
        "AND state IN ('seed', 'accepted', 'read') ORDER BY work_id",
        (project_id,),
    ).fetchall()
    summaries = work_summaries(conn, [row["work_id"] for row in rows])
    ordered = sorted(
        summaries.values(), key=lambda item: (item["year"] or 0, item["title"] or "")
    )
    used: set[str] = set()
    entries = []
    for summary in ordered:
        kind = "inproceedings" if summary["venue_type"] == "conference" else "article"
        venue_field = "booktitle" if kind == "inproceedings" else "journal"
        fields = [
            ("title", "{" + _bib_escape(summary["title"] or "Untitled") + "}"),
            ("author", " and ".join(summary["authors"]) or None),
            ("year", summary["year"]),
            (venue_field, _bib_escape(summary["venue"]) if summary["venue"] else None),
            ("doi", summary.get("doi")),
        ]
        body = ",\n".join(
            f"  {name} = {{{value}}}" for name, value in fields if value
        )
        entries.append(f"@{kind}{{{_bib_key(summary, used)},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")
