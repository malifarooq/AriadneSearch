"""Grow a project's graph within the API budget.

Levels:
    stub:     ID seen in a reference list. No API cost.
    hydrated: metadata and references fetched. Back-edges arrive with it.
    expanded: citing papers fetched. This is the step to budget for.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Literal

from ariadnesearch import references
from ariadnesearch.db import chunks, log_event, usage_today, utc_now
from ariadnesearch.errors import BadInput, NotFound
from ariadnesearch.identity import best_title_match, resolve, upsert_work
from ariadnesearch.journals import refresh_impact
from ariadnesearch.prune import prune
from ariadnesearch.topics import refresh_topics
from ariadnesearch.mine import parse_bib, resolve_entries
from ariadnesearch.openalex import MAX_OR_VALUES, OpenAlexClient
from ariadnesearch.rank import KNOWN_STATES, load_snapshot, load_weights, rank
from ariadnesearch.records import WorkRecord, arxiv_doi, normalize_doi

Decision = Literal["accepted", "rejected", "read"]

CITERS_MAX_AGE = timedelta(days=30)
MAX_NEW_BY_OVERLAP = 300  # papers added per step for shared references or citers
PER_PAPER_LIMIT = 10  # suggestions each newly added paper brings in, once
UPDATE_LOOKBACK = timedelta(days=14)  # OpenAlex adds some papers after their date
UPDATE_CAP = 2000  # citing papers per update
_DOI = re.compile(r"10\.\d{4,9}/\S+")
_ARXIV = re.compile(
    r"^(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)?"
    r"([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z.\-]+/[0-9]{7}(?:v\d+)?)(?:\.pdf)?$",
    re.IGNORECASE,
)
_OPENALEX = re.compile(r"^(?:https?://openalex\.org/)?(W\d+)$", re.IGNORECASE)


@dataclass
class SeedReport:
    added: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    no_references: list[str] = field(default_factory=list)  # titles


@dataclass
class UpdateReport:
    since: str
    citers_seen: int = 0
    new_papers: list[str] = field(default_factory=list)
    capped: bool = False
    cost_usd: float = 0.0


@dataclass
class ExpandReport:
    hydrated: int = 0
    citers_added: int = 0
    frontier_added: int = 0
    papers_expanded: int = 0
    cap_hit: list[str] = field(default_factory=list)  # shown in the UI
    refs_filled: int = 0  # known papers given reference lists from arXiv
    cost_usd: float = 0.0


def resolve_identifier(
    client: OpenAlexClient, identifier: str
) -> WorkRecord | None:
    """Find a paper from a DOI, arXiv ID, OpenAlex ID or exact title."""
    text = identifier.strip()
    openalex = _OPENALEX.match(text)
    if openalex:
        return client.work_by_id(openalex.group(1).upper())
    arxiv = _ARXIV.match(text)
    if arxiv:
        return client.work_by_doi(arxiv_doi(arxiv.group(1)))
    doi = _DOI.search(text)
    if doi:
        return client.work_by_doi(normalize_doi(doi.group(0).rstrip(".,;")))
    return best_title_match(client.search(text, per_page=5), text)


def add_member(
    conn: sqlite3.Connection, project_id: int, work_id: int, state: str
) -> bool:
    """Add a paper to a project. Seeds replace a frontier state, and nothing else does."""
    cursor = conn.execute(
        """
        INSERT INTO membership(project_id, work_id, state, ts) VALUES (?, ?, ?, ?)
        ON CONFLICT(project_id, work_id)
        DO UPDATE SET state = excluded.state, ts = excluded.ts
        WHERE membership.state = 'frontier' AND excluded.state != 'frontier'
        """,
        (project_id, work_id, state, utc_now()),
    )
    return cursor.rowcount > 0


def add_seeds(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    identifiers: list[str],
) -> SeedReport:
    """Resolve DOIs, arXiv IDs or titles and add them as seeds."""
    report = SeedReport()
    for identifier in identifiers:
        record = resolve_identifier(client, identifier)
        if record is None:
            report.unresolved.append(identifier)
            continue
        work_id = upsert_work(conn, record)
        add_member(conn, project_id, work_id, "seed")
        report.added.append(work_id)
        conn.commit()
    report.no_references = without_references(conn, report.added)
    log_event(conn, project_id, "seeds", {"count": len(report.added)})
    conn.commit()
    return report


def add_seeds_from_bib(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    bib_path: Path,
) -> SeedReport:
    """Add every resolvable `.bib` entry as a seed."""
    found, unresolved = resolve_entries(
        conn, client, list(parse_bib(bib_path).values())
    )
    for work_id in set(found.values()):
        add_member(conn, project_id, work_id, "seed")
    log_event(conn, project_id, "seeds", {"count": len(found)})
    conn.commit()
    added = sorted(set(found.values()))
    return SeedReport(added, unresolved, without_references(conn, added))


def without_references(conn: sqlite3.Connection, work_ids: list[str]) -> list[str]:
    """Return titles of papers with no OpenAlex reference list.

    Expanding such a paper can only follow the papers that cite it.
    """
    titles: list[str] = []
    for chunk in chunks(sorted(set(work_ids)), 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT title FROM work WHERE id IN ({marks}) AND COALESCE(n_refs, 0) = 0",
            chunk,
        )
        titles += [row["title"] or "Untitled" for row in rows]
    return sorted(titles)


def add_work_ids_as_seeds(
    conn: sqlite3.Connection, project_id: int, work_ids: set[str]
) -> None:
    """Add papers already in the database as seeds."""
    for work_id in work_ids:
        add_member(conn, project_id, work_id, "seed")
    log_event(conn, project_id, "seeds", {"count": len(work_ids)})
    conn.commit()


def _openalex_ids(conn: sqlite3.Connection, work_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT value FROM work_alias WHERE work_id = ? AND scheme = 'openalex'",
        (work_id,),
    )
    return [row["value"] for row in rows]


def hydrate(
    conn: sqlite3.Connection, client: OpenAlexClient, work_ids: list[int]
) -> int:
    """Fetch metadata and references for stubs. Return the number fetched.

    Papers whose stored reference list is shorter than OpenAlex's count are
    fetched again too, because pruning may have removed references they share
    with no one else. That costs one list call per 100 papers.
    """
    stubs: list[str] = []
    for chunk in chunks(work_ids, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT a.value FROM work w
            JOIN work_alias a ON a.work_id = w.id AND a.scheme = 'openalex'
            WHERE w.id IN ({marks}) AND (
                w.level = 'stub'
                OR COALESCE(w.n_refs, 0) > (SELECT COUNT(*) FROM cites c WHERE c.src = w.id)
            )
            """,
            chunk,
        )
        stubs.extend(row["value"] for row in rows)
    records = client.works_by_ids(stubs)
    for record in records:
        upsert_work(conn, record)
    conn.commit()
    return len(records)


def _citers_are_fresh(conn: sqlite3.Connection, work_id: int) -> bool:
    row = conn.execute(
        "SELECT citers_at FROM fetch_state WHERE work_id = ?", (work_id,)
    ).fetchone()
    if row is None or row["citers_at"] is None:
        return False
    fetched_at = datetime.fromisoformat(row["citers_at"])
    return datetime.now(timezone.utc) - fetched_at < CITERS_MAX_AGE


def expand_citers(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    work_id: int,
    cap: int = 500,
) -> tuple[int, bool]:
    """Fetch citing papers. Return (number stored, whether the cap was reached)."""
    if _citers_are_fresh(conn, work_id):
        row = conn.execute(
            "SELECT citers_total FROM fetch_state WHERE work_id = ?", (work_id,)
        ).fetchone()
        return 0, (row["citers_total"] or 0) > cap

    openalex_ids = _openalex_ids(conn, work_id)
    if not openalex_ids:
        return 0, False
    records, total = client.fetch_citers(openalex_ids, cap)
    now = utc_now()
    for record in records:
        citer = upsert_work(conn, record)
        current = resolve(conn, "openalex", openalex_ids[0]) or work_id
        if citer != current:
            conn.execute(
                "INSERT OR IGNORE INTO cites(src, dst) VALUES (?, ?)", (citer, current)
            )
    work_id = resolve(conn, "openalex", openalex_ids[0]) or work_id
    conn.execute(
        """
        INSERT INTO fetch_state(work_id, citers_total, citers_at) VALUES (?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
            citers_total = excluded.citers_total, citers_at = excluded.citers_at
        """,
        (work_id, total, now),
    )
    conn.execute("UPDATE work SET level = 'expanded' WHERE id = ?", (work_id,))
    conn.commit()
    return len(records), total > cap


def _known_ids(conn: sqlite3.Connection, project_id: int) -> list[str]:
    marks = ",".join("?" * len(KNOWN_STATES))
    rows = conn.execute(
        f"SELECT work_id FROM membership WHERE project_id = ? AND state IN ({marks})",
        (project_id, *KNOWN_STATES),
    )
    return [row["work_id"] for row in rows]


def _frontier_ids(conn: sqlite3.Connection, project_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT work_id FROM membership WHERE project_id = ? AND state = 'frontier'",
        (project_id,),
    )
    return [row["work_id"] for row in rows]


def _add_direct_neighbours(conn: sqlite3.Connection, project_id: int) -> int:
    """Add the references and citers of known papers to the frontier."""
    marks = ",".join("?" * len(KNOWN_STATES))
    added = 0
    for neighbour, known in (("dst", "src"), ("src", "dst")):
        cursor = conn.execute(
            f"""
            INSERT OR IGNORE INTO membership(project_id, work_id, state, ts)
            SELECT DISTINCT ?, c.{neighbour}, 'frontier', ?
            FROM cites c JOIN membership m ON m.work_id = c.{known}
            WHERE m.project_id = ? AND m.state IN ({marks})
            """,
            (project_id, utc_now(), project_id, *KNOWN_STATES),
        )
        added += max(cursor.rowcount, 0)
    return added


def _add_by_overlap(
    conn: sqlite3.Connection, project_id: int, limit: int = MAX_NEW_BY_OVERLAP
) -> int:
    """Add papers sharing at least two citers or references with known papers."""
    marks = ",".join("?" * len(KNOWN_STATES))
    queries = (
        # Co-cited: cited by at least two papers that also cite a known paper.
        ("c2.dst", "COUNT(DISTINCT c1.src)", "c1.dst = m.work_id", "c2.src = c1.src"),
        # Coupled: shares at least two references with a known paper.
        ("c2.src", "COUNT(DISTINCT c1.dst)", "c1.src = m.work_id", "c2.dst = c1.dst"),
    )
    added = 0
    for candidate, shared, join_known, join_other in queries:
        rows = conn.execute(
            f"""
            SELECT {candidate} AS candidate, {shared} AS shared
            FROM membership m
            JOIN cites c1 ON {join_known}
            JOIN cites c2 ON {join_other}
            WHERE m.project_id = ? AND m.state IN ({marks})
            AND NOT EXISTS (
                SELECT 1 FROM membership x
                WHERE x.project_id = m.project_id AND x.work_id = {candidate}
            )
            GROUP BY {candidate}
            HAVING {shared} >= 2
            ORDER BY shared DESC, candidate
            LIMIT ?
            """,
            (project_id, *KNOWN_STATES, limit),
        ).fetchall()
        for row in rows:
            added += add_member(conn, project_id, row["candidate"], "frontier")
    return added


def _neighbours(conn: sqlite3.Connection, work_id: int) -> set[int]:
    """Return a paper's references and stored citers, plus papers sharing at least
    two citers or two references with it."""
    rows = conn.execute(
        """
        SELECT dst FROM cites WHERE src = :id
        UNION SELECT src FROM cites WHERE dst = :id
        UNION SELECT * FROM (
            SELECT other.dst FROM cites mine
            JOIN cites other ON other.src = mine.src
            WHERE mine.dst = :id AND other.dst != :id
            GROUP BY other.dst HAVING COUNT(*) >= 2
        )
        UNION SELECT * FROM (
            SELECT other.src FROM cites mine
            JOIN cites other ON other.dst = mine.dst
            WHERE mine.src = :id AND other.src != :id
            GROUP BY other.src HAVING COUNT(*) >= 2
        )
        """,
        {"id": work_id},
    )
    return {row[0] for row in rows}


def _populate(
    conn: sqlite3.Connection,
    project_id: int,
    limit: int,
    only: set[str] | None = None,
) -> list[str]:
    """Add the best-scoring neighbours of known papers to the frontier. Return them.

    Each known paper adds up to `limit` papers once, the first time it is
    populated, and is then recorded in `populated`. With `only`, every known paper
    may add up to `limit` papers from that set, as a weekly update does.
    """
    members = dict(
        conn.execute(
            "SELECT work_id, state FROM membership WHERE project_id = ?", (project_id,)
        ).fetchall()
    )
    sources = _known_ids(conn, project_id)
    if only is None:
        done = {
            row[0]
            for row in conn.execute(
                "SELECT work_id FROM populated WHERE project_id = ?", (project_id,)
            )
        }
        sources = [work_id for work_id in sources if work_id not in done]
    neighbours = {
        source: {
            work_id
            for work_id in _neighbours(conn, source)
            if work_id not in members and (only is None or work_id in only)
        }
        for source in sorted(sources)
    }
    pool = set().union(*neighbours.values())

    added: list[str] = []
    if pool:
        snapshot, state = load_snapshot(conn, project_id, candidates=pool)
        scores = {
            item.work_id: item.score
            for item in rank(snapshot, state, load_weights(conn, project_id))
        }
        def best_first(work_id: int) -> tuple[float, int]:
            return -scores.get(work_id, 0.0), work_id

        for linked in neighbours.values():
            added.extend(sorted(linked - set(added), key=best_first)[:limit])
        for work_id in added:
            add_member(conn, project_id, work_id, "frontier")
    if only is None:
        now = utc_now()
        conn.executemany(
            "INSERT OR IGNORE INTO populated(project_id, work_id, at) VALUES (?, ?, ?)",
            [(project_id, source, now) for source in sources],
        )
    return added


def pending_count(conn: sqlite3.Connection, project_id: int) -> int:
    """Return how many known papers have not added their neighbours yet."""
    marks = ",".join("?" * len(KNOWN_STATES))
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM membership m
        WHERE m.project_id = ? AND m.state IN ({marks})
        AND NOT EXISTS (
            SELECT 1 FROM populated p
            WHERE p.project_id = m.project_id AND p.work_id = m.work_id
        )
        """,
        (project_id, *KNOWN_STATES),
    ).fetchone()
    return int(row[0])


def expand_project(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    top_k: int = 20,
    citer_cap: int = 500,
    per_paper: int | None = PER_PAPER_LIMIT,
) -> ExpandReport:
    """Run one expansion round.

    1. Hydrate known papers, fill missing reference lists from arXiv, and fetch
       their citers.
    With `per_paper`, the default:
    2. Add each known paper's best neighbours, up to `per_paper` open ones each.
    3. Hydrate them.
    With `per_paper=None`, the full round used by the evaluation scripts:
    2. Add all neighbours of known papers, plus papers overlapping with them.
    3. Hydrate the frontier.
    4. Fetch citers for the top-k frontier papers only.
    5. Add newly overlapping papers, and hydrate them.
    The citer cap is reported wherever it was reached.
    """
    report = ExpandReport()
    spent_before = usage_today(conn, "openalex")[1]
    titles_capped: list[str] = []

    def fetch(work_id: int) -> None:
        added, capped = expand_citers(conn, client, work_id, citer_cap)
        report.citers_added += added
        report.papers_expanded += 1 if added else 0
        if capped:
            titles_capped.append(work_id)

    known = _known_ids(conn, project_id)
    report.hydrated += hydrate(conn, client, known)
    report.refs_filled = references.fill_from_arxiv(conn, client, known)
    for work_id in known:
        fetch(work_id)

    if per_paper is not None:
        report.frontier_added += len(_populate(conn, project_id, per_paper))
        conn.commit()
        report.hydrated += hydrate(conn, client, _frontier_ids(conn, project_id))
        return _finish_expand(
            conn, client, project_id, report, known, titles_capped, spent_before
        )

    report.frontier_added += _add_direct_neighbours(conn, project_id)
    report.frontier_added += _add_by_overlap(conn, project_id)
    conn.commit()
    report.hydrated += hydrate(conn, client, _frontier_ids(conn, project_id))

    snapshot, state = load_snapshot(conn, project_id)
    ranked = rank(snapshot, state, load_weights(conn, project_id))
    unexpanded = (
        item.work_id for item in ranked if not _citers_are_fresh(conn, item.work_id)
    )
    for work_id in list(islice(unexpanded, top_k)):
        fetch(work_id)

    report.frontier_added += _add_by_overlap(conn, project_id)
    conn.commit()
    report.hydrated += hydrate(conn, client, _frontier_ids(conn, project_id))
    return _finish_expand(
        conn, client, project_id, report, known, titles_capped, spent_before
    )


def _finish_expand(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    report: ExpandReport,
    known: list[str],
    titles_capped: list[str],
    spent_before: float,
) -> ExpandReport:
    members = known + _frontier_ids(conn, project_id)
    refresh_impact(conn, client, members)
    refresh_topics(conn, client, members)
    prune(conn)
    report.cap_hit = sorted(set(titles_capped))
    report.cost_usd = usage_today(conn, "openalex")[1] - spent_before
    log_event(
        conn,
        project_id,
        "expand",
        {
            "hydrated": report.hydrated,
            "citers_added": report.citers_added,
            "frontier_added": report.frontier_added,
            "cost_usd": report.cost_usd,
        },
    )
    conn.commit()
    return report


def _last_run(conn: sqlite3.Connection, project_id: int) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM event WHERE project_id = ? "
        "AND type IN ('expand', 'update')",
        (project_id,),
    ).fetchone()
    return datetime.fromisoformat(row["ts"]) if row["ts"] else None


def update_project(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    since: date | None = None,
    cap: int = UPDATE_CAP,
    per_paper: int | None = PER_PAPER_LIMIT,
) -> UpdateReport:
    """Add papers that cite the known papers and were published since the last run.

    Known papers are asked about 100 at a time, so a weekly update costs a few list
    calls. `since` defaults to the last expand or update, less UPDATE_LOOKBACK, so
    papers OpenAlex indexes later than that need an earlier `since`.
    With `per_paper`, each known paper adds at most that many of its new citers,
    best first. All of them are stored, so they still inform scores.
    """
    if since is None:
        last = _last_run(conn, project_id)
        if last is None:
            raise BadInput("Run expand once before update.")
        since = (last - UPDATE_LOOKBACK).date()
    report = UpdateReport(since.isoformat())
    spent_before = usage_today(conn, "openalex")[1]
    fetched: set[str] = set()

    openalex_ids = sorted(
        {
            value
            for work_id in _known_ids(conn, project_id)
            for value in _openalex_ids(conn, work_id)
        }
    )
    for chunk in chunks(openalex_ids, MAX_OR_VALUES):
        room = cap - report.citers_seen
        if room <= 0:
            report.capped = True
            break
        records, total = client.fetch_citers(list(chunk), room, since=since)
        report.capped |= total > len(records)
        report.citers_seen += len(records)
        for record in records:
            fetched.add(upsert_work(conn, record))
        conn.commit()
    if per_paper is None:
        report.new_papers = [
            work_id
            for work_id in sorted(fetched)
            if add_member(conn, project_id, work_id, "frontier")
        ]
    else:
        report.new_papers = _populate(conn, project_id, per_paper, only=fetched)
    conn.commit()
    refresh_impact(conn, client, report.new_papers)
    prune(conn)

    report.cost_usd = usage_today(conn, "openalex")[1] - spent_before
    log_event(
        conn,
        project_id,
        "update",
        {
            "since": report.since,
            "citers_seen": report.citers_seen,
            "new_papers": len(report.new_papers),
            "cost_usd": report.cost_usd,
        },
    )
    conn.commit()
    return report


def record_decision(
    conn: sqlite3.Connection,
    project_id: int,
    work_id: int,
    decision: Decision,
) -> None:
    """Update `membership` and append the decision to `event`."""
    cursor = conn.execute(
        "UPDATE membership SET state = ?, ts = ? WHERE project_id = ? AND work_id = ?",
        (decision, utc_now(), project_id, work_id),
    )
    if cursor.rowcount == 0:
        raise NotFound(f"Paper {work_id} is not in this project.")
    log_event(conn, project_id, "decision", {"work_id": work_id, "decision": decision})
    conn.commit()

