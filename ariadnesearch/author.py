"""Author projects: one researcher's papers, how they connect over time, and
the work they draw on.

An author project keeps one person's papers as its seeds. Fetching them costs
one list call per 100 papers. The papers they cite are then described with one
more call per 100: titles, authors, years and topics, without those papers' own
reference lists. The papers they cite most join the project to review, as their
inspirations.

A cited paper that lists the author under a name their papers use is counted
as their own paper missing from the profile, so its links to their other papers
show. Someone else with the same name is counted too, which matters for common
names: the overview lists these papers, and marking one as not theirs rejects it.

Their own papers never fetch citing papers or add neighbours when the graph is
updated (see expand.growing_states). Papers you accept grow it as usual.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

from ariadnesearch.db import chunks, create_project, log_event, usage_today, utc_now
from ariadnesearch.errors import BadInput, NotFound
from ariadnesearch.expand import add_member, hydrate
from ariadnesearch.identity import describe_work, upsert_work
from ariadnesearch.journals import refresh_impact
from ariadnesearch.openalex import AuthorProfile, OpenAlexClient
from ariadnesearch.prune import prune
from ariadnesearch.records import normalize_title
from ariadnesearch.views import SEARCH_AUTHORS, _short_label, work_summaries

WORKS_CAP = 2000  # their papers fetched, most cited first
DESCRIBE_CAP = 5000  # papers they cite that get titles and authors, most cited first
INSPIRATIONS = 200  # papers they cite that join the project to review
SKIPPED_TYPES = frozenset(
    {
        "dataset",
        "editorial",
        "erratum",
        "grant",
        "letter",
        "libguides",
        "paratext",
        "peer-review",
        "retraction",
        "supplementary-materials",
    }
)
LISTED = 60  # co-authors and inspiring authors returned
SHOWN_WORKS = 8  # an inspiring author's works listed, the most cited by the author first
_ORCID = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.IGNORECASE)
_AUTHOR_ID = re.compile(r"^(?:https?://openalex\.org/)?(A\d+)$", re.IGNORECASE)


@dataclass
class AuthorReport:
    papers: int = 0  # their papers in the project
    new_papers: list[int] = field(default_factory=list)
    total: int = 0  # papers OpenAlex lists for them
    capped: bool = False
    described: int = 0  # cited papers given titles and authors
    claimed: int = 0  # cited papers that list them, added to their papers
    inspirations: int = 0  # cited papers added to review
    cost_usd: float = 0.0


def find_authors(
    client: OpenAlexClient, query: str, limit: int = 8
) -> list[AuthorProfile]:
    """Find author profiles by name, ORCID or OpenAlex ID.

    A name costs as much as a search. Profiles whose name holds every word of
    the query come first, the most prolific first, because OpenAlex's own order
    can favour records that list several people as one name.
    """
    text = query.strip()
    if not text:
        raise BadInput("Enter an author's name, ORCID or OpenAlex ID.")
    orcid = _ORCID.search(text)
    openalex = _AUTHOR_ID.match(text)
    if orcid or openalex:
        identifier = f"orcid:{orcid.group(1).upper()}" if orcid else openalex.group(1).upper()
        found = client.author(identifier)
        return [found] if found else []

    words = set(normalize_title(text).split())

    def full_match(profile: AuthorProfile) -> bool:
        return any(
            words <= set(normalize_title(name).split())
            for name in (profile.name, *profile.alternatives)
        )

    found = client.search_authors(text, per_page=max(limit, 10))
    ranked = sorted(
        found,
        key=lambda profile: (
            (False, -profile.works_count) if full_match(profile) else (True, 0)
        ),
    )
    return ranked[:limit]


def _profiles(client: OpenAlexClient, author_ids: list[str]) -> list[AuthorProfile]:
    profiles = []
    for raw in dict.fromkeys(item.strip() for item in author_ids if item.strip()):
        match = _AUTHOR_ID.match(raw)
        if not match:
            raise BadInput(f"Not an OpenAlex author ID: {raw}")
        profile = client.author(match.group(1).upper())
        if profile is None:
            raise NotFound(f"OpenAlex has no author {raw}.")
        profiles.append(profile)
    if not profiles:
        raise BadInput("Choose at least one author profile.")
    return profiles


def _save_profiles(
    conn: sqlite3.Connection, project_id: int, profiles: list[AuthorProfile]
) -> None:
    for profile in profiles:
        public = {
            key: value for key, value in asdict(profile).items() if key != "alternatives"
        }
        conn.execute(
            """
            INSERT INTO project_author(project_id, author_id, name, names_json, profile_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, author_id) DO UPDATE SET
                name = excluded.name, profile_json = excluded.profile_json
            """,
            (
                project_id,
                profile.id,
                profile.name,
                json.dumps(sorted({profile.name, *profile.alternatives})),
                json.dumps(public),
            ),
        )
    conn.commit()


def create_author_project(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    author_ids: list[str],
    name: str | None = None,
) -> tuple[int, AuthorReport]:
    """Create a project from one person's author profiles and fetch their papers.

    Pass several profiles when OpenAlex has split one person across them. The
    project is named after the first unless `name` is given.
    """
    profiles = _profiles(client, author_ids)
    project_id = create_project(conn, name or profiles[0].name)
    _save_profiles(conn, project_id, profiles)
    log_event(conn, project_id, "author", {"profiles": [item.id for item in profiles]})
    return project_id, update_author(conn, client, project_id)


def add_profiles(
    conn: sqlite3.Connection, client: OpenAlexClient, project_id: int, author_ids: list[str]
) -> AuthorReport:
    """Follow more profiles of the same person, and fetch their papers."""
    if not _project_profiles(conn, project_id):
        raise BadInput("This project does not follow an author.")
    _save_profiles(conn, project_id, _profiles(client, author_ids))
    return update_author(conn, client, project_id)


def _project_profiles(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM project_author WHERE project_id = ? ORDER BY author_id",
        (project_id,),
    ).fetchall()


def _own_ids(conn: sqlite3.Connection, project_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT work_id FROM membership WHERE project_id = ? AND state = 'seed'",
        (project_id,),
    )
    return [row[0] for row in rows]


def update_author(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    cap: int = WORKS_CAP,
) -> AuthorReport:
    """Fetch the author's papers again, add new ones, and describe what they cite.

    Fetching everything again also refreshes their citation counts, and costs one
    list call per 100 papers.
    """
    profiles = _project_profiles(conn, project_id)
    if not profiles:
        raise BadInput("This project does not follow an author.")
    report = AuthorReport()
    spent_before = usage_today(conn, "openalex")[1]
    ids = [row["author_id"] for row in profiles]
    records, report.total = client.works_by_authors(ids, cap)
    report.capped = report.total > len(records)

    seen_names: dict[str, set[str]] = defaultdict(set)
    now = utc_now()
    for record in records:
        if record.type in SKIPPED_TYPES:
            continue
        for name, author_id in zip(record.authors, record.author_ids):
            if author_id in ids:
                seen_names[author_id].add(name)
        work_id = upsert_work(conn, record)
        if add_member(conn, project_id, work_id, "seed"):
            report.new_papers.append(work_id)
        conn.execute(
            "INSERT OR IGNORE INTO populated(project_id, work_id, at) VALUES (?, ?, ?)",
            (project_id, work_id, now),
        )
    for row in profiles:
        names = set(json.loads(row["names_json"])) | seen_names[row["author_id"]]
        conn.execute(
            "UPDATE project_author SET names_json = ?, fetched_at = ? "
            "WHERE project_id = ? AND author_id = ?",
            (json.dumps(sorted(names)), now, project_id, row["author_id"]),
        )
    conn.commit()

    report.described = _describe_cited(conn, client, project_id)
    report.claimed = len(_claim_listed(conn, client, project_id))
    if report.claimed:
        report.described += _describe_cited(conn, client, project_id)  # what those cite
    added = _add_inspirations(conn, project_id)
    report.inspirations = len(added)
    own = _own_ids(conn, project_id)
    refresh_impact(conn, client, own + added)
    prune(conn)
    report.papers = len(own)
    report.cost_usd = usage_today(conn, "openalex")[1] - spent_before
    log_event(
        conn,
        project_id,
        "author_update",
        {
            "new_papers": len(report.new_papers),
            "described": report.described,
            "claimed": report.claimed,
            "cost_usd": report.cost_usd,
        },
    )
    conn.commit()
    return report


def _describe_cited(
    conn: sqlite3.Connection, client: OpenAlexClient, project_id: int
) -> int:
    """Give titles and authors to papers the author cites, most cited first."""
    rows = conn.execute(
        """
        SELECT a.value FROM cites c
        JOIN membership m ON m.work_id = c.src AND m.project_id = ? AND m.state = 'seed'
        JOIN work w ON w.id = c.dst
        JOIN work_alias a ON a.work_id = c.dst AND a.scheme = 'openalex'
        WHERE w.title IS NULL
        GROUP BY c.dst
        ORDER BY COUNT(DISTINCT c.src) DESC, c.dst
        LIMIT ?
        """,
        (project_id, DESCRIBE_CAP),
    ).fetchall()
    records = client.describe_works([row[0] for row in rows])
    for record in records:
        describe_work(conn, record)
    conn.commit()
    return len(records)


def _add_inspirations(conn: sqlite3.Connection, project_id: int) -> list[int]:
    """Add the papers the author cites most to the project, for review. Return them."""
    own_names = _names(_project_profiles(conn, project_id))
    rows = conn.execute(
        """
        SELECT c.dst, MAX(w.authors) AS authors FROM cites c
        JOIN membership m ON m.work_id = c.src AND m.project_id = :project AND m.state = 'seed'
        JOIN work w ON w.id = c.dst
        WHERE NOT EXISTS (
            SELECT 1 FROM membership own
            WHERE own.project_id = :project AND own.work_id = c.dst AND own.state = 'seed'
        )
        GROUP BY c.dst
        ORDER BY COUNT(DISTINCT c.src) DESC, COALESCE(MAX(w.oa_cited_by), 0) DESC, c.dst
        """,
        {"project": project_id},
    )
    chosen = [
        row["dst"]
        for row in rows
        if not own_names & set(json.loads(row["authors"] or "[]"))  # theirs, see _claim_listed
    ][:INSPIRATIONS]
    added = [work_id for work_id in chosen if add_member(conn, project_id, work_id, "frontier")]
    conn.commit()
    return added


def _claim_listed(conn: sqlite3.Connection, client: OpenAlexClient, project_id: int) -> list[int]:
    """Add cited papers that list the author to their papers. Return them.

    OpenAlex sometimes credits a paper to another profile of the same person,
    so it is missing from the one followed. A paper marked as not theirs is
    rejected, and stays out.
    """
    own_names = _names(_project_profiles(conn, project_id))
    rows = conn.execute(
        """
        SELECT DISTINCT w.id, w.authors FROM cites c
        JOIN membership m ON m.work_id = c.src AND m.project_id = :project AND m.state = 'seed'
        JOIN work w ON w.id = c.dst
        WHERE w.authors IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM membership x
            WHERE x.project_id = :project AND x.work_id = c.dst
            AND x.state IN ('seed', 'rejected')
        )
        """,
        {"project": project_id},
    ).fetchall()
    listed = [row["id"] for row in rows if own_names & set(json.loads(row["authors"]))]
    if not listed:
        return []
    hydrate(conn, client, listed)
    now = utc_now()
    claimed = []
    for work_id in listed:
        work_id = _current_id(conn, work_id)  # hydrating can merge versions
        if add_member(conn, project_id, work_id, "seed"):
            claimed.append(work_id)
        conn.execute(
            "INSERT OR IGNORE INTO populated(project_id, work_id, at) VALUES (?, ?, ?)",
            (project_id, work_id, now),
        )
    log_event(conn, project_id, "author_claim", {"work_ids": claimed})
    conn.commit()
    return claimed


def _current_id(conn: sqlite3.Connection, work_id: int) -> int:
    """Follow merges from a paper ID that may have been merged away."""
    while conn.execute("SELECT 1 FROM work WHERE id = ?", (work_id,)).fetchone() is None:
        row = conn.execute(
            "SELECT keep_id FROM merge_log WHERE drop_id = ? AND undone_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (work_id,),
        ).fetchone()
        if row is None:
            break
        work_id = row[0]
    return work_id


def _claimed_ids(conn: sqlite3.Connection, project_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT payload_json FROM event WHERE project_id = ? AND type = 'author_claim'",
        (project_id,),
    )
    return {work_id for row in rows for work_id in json.loads(row[0])["work_ids"]}


# The analysis


def _names(profiles: list[sqlite3.Row]) -> set[str]:
    return {name for row in profiles for name in json.loads(row["names_json"])}


def _brief(summary: dict, **extra) -> dict:
    return {
        "id": summary["id"],
        "title": summary["title"] or "Untitled",
        "label": _short_label(summary),
        "year": summary["year"],
        **extra,
    }


def author_view(conn: sqlite3.Connection, project_id: int) -> dict:
    """Describe the author's output over time and the work it draws on.

    Inspirations are the papers they cite, counted by how many of their papers
    cite each one, with the years of the first and last of those.
    """
    profiles = _project_profiles(conn, project_id)
    if not profiles:
        raise NotFound("This project does not follow an author.")
    own_names = _names(profiles)
    own_ids = _own_ids(conn, project_id)
    own = work_summaries(conn, own_ids)
    order = sorted(
        own.values(), key=lambda item: (item["year"] or 0, item["pub_date"] or "", item["id"])
    )

    cited_by_own: dict[int, list[int]] = defaultdict(list)  # cited paper -> their papers
    self_links = 0
    for chunk in chunks(own_ids, 500):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT src, dst FROM cites WHERE src IN ({marks})", chunk):
            if row["dst"] in own:
                self_links += 1
            else:
                cited_by_own[row["dst"]].append(row["src"])
    cited = _cited_summaries(conn, list(cited_by_own))
    states = dict(
        conn.execute(
            "SELECT work_id, state FROM membership WHERE project_id = ?", (project_id,)
        ).fetchall()
    )
    # A cited paper listing the author is their own, missing from the profile,
    # unless it was marked as not theirs. Most are added at each update.
    missing_own = {
        work_id: cited_by_own.pop(work_id)
        for work_id, summary in list(cited.items())
        if own_names & set(summary["authors"]) and states.get(work_id) != "rejected"
    }
    self_links += sum(len(citers) for citers in missing_own.values())

    def years_of(citers: list[int]) -> list[int]:
        return sorted(own[citer]["year"] for citer in citers if own[citer]["year"])

    inspirations = []
    for work_id, citers in cited_by_own.items():
        summary = cited.get(work_id)
        if summary is None or not summary["title"]:
            continue
        years = years_of(citers)
        inspirations.append(
            {
                **_brief(summary),
                "authors": summary["authors"][:SEARCH_AUTHORS],
                "cited_by": summary["cited_by"],
                "topic": summary["topic"],
                "count": len(set(citers)),
                "first_year": years[0] if years else None,
                "last_year": years[-1] if years else None,
            }
        )
    inspirations.sort(key=lambda item: (-item["count"], -(item["cited_by"] or 0), item["id"]))
    for item in inspirations:
        item["state"] = states.get(item["id"])

    missing = sorted(
        (
            _brief(cited[work_id], count=len(set(citers)))
            for work_id, citers in missing_own.items()
            if cited[work_id]["title"]
        ),
        key=lambda item: (-item["count"], item["id"]),
    )
    coauthors = _coauthors(order, own_names)
    years = [item["year"] for item in order if item["year"]]
    per_year = []
    if years:
        counts = Counter(years)
        citations = Counter()
        for item in order:
            if item["year"]:
                citations[item["year"]] += item["cited_by"] or 0
        per_year = [
            {"year": year, "papers": counts[year], "citations": citations[year]}
            for year in range(years[0], years[-1] + 1)
        ]
    profile_list = sorted(
        ({**json.loads(row["profile_json"]), "fetched_at": row["fetched_at"]} for row in profiles),
        key=lambda item: -(item.get("works_count") or 0),
    )
    return {
        "name": profile_list[0]["name"],
        "profiles": profile_list,
        "papers": len(own),
        "first_year": years[0] if years else None,
        "last_year": years[-1] if years else None,
        "citations": sum(item["cited_by"] or 0 for item in order),
        "self_links": self_links,
        "per_year": per_year,
        "inspirations": inspirations[:INSPIRATIONS],
        "inspiring_authors": _inspiring_authors(
            cited_by_own, cited, own, own_names, {item["name"] for item in coauthors}
        ),
        "coauthors": coauthors[:LISTED],
        "missing_own": missing[:LISTED],
        "claimed": [
            _brief(own[work_id])
            for work_id in sorted(_claimed_ids(conn, project_id) & set(own), key=lambda w: own[w]["year"] or 0)
        ],
    }


def _cited_summaries(conn: sqlite3.Connection, work_ids: list[int]) -> dict[int, dict]:
    """Return title, authors, year and topic for cited papers, cheaply."""
    found: dict[int, dict] = {}
    for chunk in chunks(work_ids, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, title, authors, year, oa_cited_by, topic FROM work WHERE id IN ({marks})",
            chunk,
        )
        for row in rows:
            found[row["id"]] = {
                "id": row["id"],
                "title": row["title"],
                "authors": json.loads(row["authors"] or "[]"),
                "year": row["year"],
                "cited_by": row["oa_cited_by"],
                "topic": row["topic"] or None,
            }
    return found


def _coauthors(order: list[dict], own_names: set[str]) -> list[dict]:
    people: dict[str, dict] = {}
    for item in order:
        for name in item["authors"]:
            if name in own_names:
                continue
            entry = people.setdefault(
                name,
                {"name": name, "papers": 0, "first_year": None, "last_year": None, "work_ids": []},
            )
            entry["papers"] += 1
            entry["work_ids"].append(item["id"])
            if item["year"]:
                entry["first_year"] = min(filter(None, (entry["first_year"], item["year"])))
                entry["last_year"] = max(filter(None, (entry["last_year"], item["year"])))
    return sorted(people.values(), key=lambda entry: (-entry["papers"], entry["name"]))


def _inspiring_authors(
    cited_by_own: dict[int, list[int]],
    cited: dict[int, dict],
    own: dict[int, dict],
    own_names: set[str],
    coauthors: set[str],
) -> list[dict]:
    """Count, for each cited author, how many of the author's papers cite their work."""
    people: dict[str, dict] = {}
    for work_id, citers in cited_by_own.items():
        for name in (cited.get(work_id) or {}).get("authors") or []:
            if name in own_names:
                continue
            entry = people.setdefault(name, {"citing": set(), "works": set()})
            entry["citing"].update(citers)
            entry["works"].add(work_id)
    rows = []
    for name, entry in people.items():
        years = sorted(own[citer]["year"] for citer in entry["citing"] if own[citer]["year"])
        rows.append(
            {
                "name": name,
                "papers": len(entry["citing"]),
                "works": len(entry["works"]),
                "first_year": years[0] if years else None,
                "last_year": years[-1] if years else None,
                "coauthor": name in coauthors,
                "work_ids": sorted(entry["works"]),
            }
        )
    rows.sort(key=lambda row: (-row["papers"], -row["works"], row["name"]))
    for row in rows[:LISTED]:
        row["works_cited"] = sorted(
            (
                _brief(cited[work_id], count=len(set(cited_by_own[work_id])))
                for work_id in row["work_ids"]
                if cited[work_id]["title"]
            ),
            key=lambda item: (-item["count"], item["id"]),
        )[:SHOWN_WORKS]
    return rows[:LISTED]


def author_context(conn: sqlite3.Connection, project_id: int, work_id: int) -> dict | None:
    """Place one paper in the author's work, for the details panel.

    For their own paper: their earlier papers it builds on, their later papers
    that build on it, the cited papers that first appear in it, and whether it was
    added from their reference lists. For a paper they cite: which of their papers
    cite it, and when.
    """
    profiles = _project_profiles(conn, project_id)
    if not profiles:
        return None
    own_names = _names(profiles)
    own_ids = _own_ids(conn, project_id)
    own = work_summaries(conn, own_ids)
    order = sorted(
        own.values(), key=lambda item: (item["year"] or 0, item["pub_date"] or "", item["id"])
    )
    position = {item["id"]: index for index, item in enumerate(order)}

    def by_time(ids) -> list[dict]:
        return [_brief(own[item]) for item in sorted(set(ids), key=position.get)]

    if work_id not in own:
        citers = [
            row[0]
            for row in conn.execute("SELECT src FROM cites WHERE dst = ?", (work_id,))
            if row[0] in own
        ]
        return {"kind": "cited", "papers": by_time(citers)}

    refs = [row[0] for row in conn.execute("SELECT dst FROM cites WHERE src = ?", (work_id,))]
    later = [
        row[0]
        for row in conn.execute("SELECT src FROM cites WHERE dst = ?", (work_id,))
        if row[0] in own
    ]
    others = [ref for ref in refs if ref not in own]
    citing: dict[int, set[int]] = defaultdict(set)
    for chunk in chunks(others, 500):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT src, dst FROM cites WHERE dst IN ({marks})", chunk
        ):
            if row["src"] in own:
                citing[row["dst"]].add(row["src"])
    new_here = [
        ref
        for ref in others
        if min(citing[ref] or {work_id}, key=position.get) == work_id
    ]
    summaries = _cited_summaries(conn, new_here)
    adopted = sorted(
        (
            ref
            for ref in new_here
            if (summaries.get(ref) or {}).get("title")
            and not own_names & set(summaries[ref]["authors"])
        ),
        key=lambda ref: (-len(citing[ref]), ref),
    )
    return {
        "kind": "own",
        "claimed": work_id in _claimed_ids(conn, project_id),  # missing from the profile
        "builds_on": by_time(ref for ref in refs if ref in own),
        "built_on_by": by_time(later),
        "new_here": [
            _brief(summaries[ref], count=len(citing[ref])) for ref in adopted[:8]
        ],
        "new_total": len(adopted),
    }
