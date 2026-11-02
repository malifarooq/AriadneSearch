"""Venue ranks from a manual ICORE export, used as a label, a filter and a score term.

CORE covers computer science conferences. Most journals and physics venues have
no rank, so a missing rank means unknown, and `rank.VENUE_BONUS` treats it as neutral.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from ariadnesearch.db import chunks, log_event
from ariadnesearch.errors import BadInput, NotFound, UpstreamError
from ariadnesearch.expand import add_member
from ariadnesearch.identity import upsert_work
from ariadnesearch.openalex import OpenAlexClient, VenueCandidate
from ariadnesearch.records import normalize_title

RANK_ORDER = ("A*", "A", "B", "C")
TARGET_EDITION = "target"  # the edition shown for ranks from your own list
CORE_EDITION = "ICORE2026"  # the newest edition on the CORE portal in November 2026
CORE_EXPORT = (
    "https://portal.core.edu.au/conf-ranks/?search=&by=all&source={edition}"
    "&sort=atitle&page=1&do=Export"
)
MIN_ACRONYM_WORD_OVERLAP = 0.5
MIN_WORD_MATCH = 0.8  # share of distinctive words two names must have in common
SAMPLE_ROWS = 200

_RANK_VALUE = re.compile(
    r"^(A\*|A|B|C|Unranked|TBR|Other|Multiconference|Journal Published"
    r"|Australasian [A-C]|National.*|Regional.*)$",
    re.IGNORECASE,
)
_EDITION_VALUE = re.compile(r"^(ICORE|CORE|ERA)\d{4}$", re.IGNORECASE)
_PARENTHESES = re.compile(r"\(([^)]*)\)")
_ACRONYM = re.compile(r"^[A-Z][A-Za-z0-9&/+-]{1,14}$")
_BOILERPLATE = re.compile(
    r"\b(proceedings of the|proceedings of|proceedings|ieee|acm|annual"
    r"|\d{4}|\d+(st|nd|rd|th))\b",
    re.IGNORECASE,
)
_COMMON_WORDS = {
    "a", "advances", "an", "and", "conference", "for", "in", "international", "of",
    "on", "society", "symposium", "the", "workshop",
}


@dataclass
class VenueRank:
    rank: str
    edition: str


@dataclass
class _CoreTitle:
    title: str
    acronym: str | None
    words: set[str]


@dataclass
class CoreColumns:
    title: int
    acronym: int | None
    rank: int
    edition: int | None
    has_header: bool


def _read_rows(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]


def _detect_columns(rows: list[list[str]]) -> CoreColumns:
    """Find columns by header name, or by their values when there is no header."""
    first = [cell.strip().lower() for cell in rows[0]]
    if "title" in first and "rank" in first:
        return CoreColumns(
            title=first.index("title"),
            acronym=first.index("acronym") if "acronym" in first else None,
            rank=first.index("rank"),
            edition=first.index("source") if "source" in first else None,
            has_header=True,
        )

    sample = rows[: SAMPLE_ROWS]
    width = max(len(row) for row in sample)
    columns = [[row[i].strip() if i < len(row) else "" for row in sample] for i in range(width)]

    def share(values: list[str], pattern: re.Pattern) -> float:
        filled = [value for value in values if value]
        if not filled:
            return 0.0
        return sum(bool(pattern.match(value)) for value in filled) / len(filled)

    rank_scores = [share(values, _RANK_VALUE) for values in columns]
    edition_scores = [share(values, _EDITION_VALUE) for values in columns]
    rank_column = max(range(width), key=rank_scores.__getitem__)
    edition_column = max(range(width), key=edition_scores.__getitem__)
    if rank_scores[rank_column] < 0.8:
        raise BadInput(
            "Could not find the rank column in this CSV. "
            "Send the first few rows so the importer can be updated."
        )

    lengths = [
        sum(len(value) for value in values) / max(len(values), 1)
        for values in columns
    ]
    has_edition = edition_scores[edition_column] >= 0.8
    taken = {rank_column, edition_column} if has_edition else {rank_column}
    text_columns = [i for i in range(width) if i not in taken]
    title_column = max(text_columns, key=lengths.__getitem__)
    next_column = title_column + 1
    acronym_column = next_column if next_column in text_columns else None
    return CoreColumns(
        title=title_column,
        acronym=acronym_column,
        rank=rank_column,
        edition=edition_column if has_edition else None,
        has_header=False,
    )


def import_core_csv(
    conn: sqlite3.Connection, path: Path, edition: str | None = None
) -> int:
    """Load an export into `venue_rank`. Return the number of rows."""
    rows = _read_rows(path)
    if not rows:
        raise BadInput(f"{path.name} is empty.")
    columns = _detect_columns(rows)
    if columns.edition is None and edition is None:
        raise BadInput("This CSV has no edition column. Pass --edition, such as ICORE2026.")

    count = 0
    for row in rows[1 if columns.has_header else 0 :]:
        if len(row) <= max(columns.title, columns.rank):
            continue
        title = row[columns.title].strip()
        rank = row[columns.rank].strip()
        if not title or not rank:
            continue
        acronym = None
        if columns.acronym is not None and columns.acronym < len(row):
            acronym = row[columns.acronym].strip() or None
        row_edition = edition or row[columns.edition].strip()
        conn.execute(
            "INSERT OR REPLACE INTO venue_rank(edition, title, acronym, rank) "
            "VALUES (?, ?, ?, ?)",
            (row_edition, title, acronym, rank),
        )
        count += 1
    conn.execute("DELETE FROM venue_alias WHERE method != 'manual'")
    conn.commit()
    return count


def download_core_csv(
    edition: str, folder: Path, http: httpx.Client | None = None
) -> Path:
    """Save the CORE portal's CSV export of one edition into `folder`. Return its path.

    Each user downloads their own copy, since CORE's terms govern sharing it.
    """
    if not _EDITION_VALUE.match(edition):
        raise BadInput(f"Not a CORE edition: {edition}. Use a name such as {CORE_EDITION}.")
    client = http or httpx.Client(timeout=60, follow_redirects=True)
    try:
        response = client.get(CORE_EXPORT.format(edition=edition))
    except httpx.TransportError as exc:
        raise UpstreamError("The CORE portal did not respond. Try again later.") from exc
    finally:
        if http is None:
            client.close()
    if response.status_code != 200 or "csv" not in response.headers.get("content-type", ""):
        raise UpstreamError(
            f"The CORE portal did not return a CSV for {edition} (HTTP {response.status_code})."
        )
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{edition.lower()}.csv"
    path.write_bytes(response.content)
    return path


def _clean_name(name: str) -> str:
    """Remove year, ordinal and proceedings wording from a venue name."""
    text = _PARENTHESES.sub(" ", name)
    text = _BOILERPLATE.sub(" ", text)
    return normalize_title(text)


def _acronym_candidates(raw_name: str) -> list[str]:
    candidates = [
        group.strip()
        for group in _PARENTHESES.findall(raw_name)
        if _ACRONYM.match(group.strip())
    ]
    if _ACRONYM.match(raw_name.strip()):
        candidates.append(raw_name.strip())
    return candidates


def _distinctive(key: str) -> set[str]:
    return set(key.split()) - _COMMON_WORDS


def _word_overlap(a: str, b: str) -> float:
    """Return the share of distinctive words two venue names have in common."""
    words_a = _distinctive(a)
    words_b = _distinctive(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def _titles_by_key(conn: sqlite3.Connection) -> dict[str, _CoreTitle]:
    rows = conn.execute("SELECT DISTINCT title, acronym FROM venue_rank")
    index = {}
    for row in rows:
        key = _clean_name(row["title"])
        index[key] = _CoreTitle(row["title"], row["acronym"], _distinctive(key))
    return index


def _name_variants(raw_name: str) -> list[str]:
    """OpenAlex sometimes joins two names with "/". Try the whole, then each part."""
    parts = [part.strip() for part in raw_name.split("/")]
    return [raw_name] + [part for part in parts if len(part.split()) >= 3]


def _closest_title(
    index: dict[str, _CoreTitle], key: str, acronyms: set[str]
) -> str | None:
    """Return the one title sharing nearly all distinctive words, or None.

    Whole words are compared, so names differing by one word such as "Midwest"
    do not match. A candidate whose acronym differs from the name's is skipped.
    """
    words = _distinctive(key)
    if len(words) < 2:
        return None
    scored = sorted(
        (
            (len(words & entry.words) / len(words | entry.words), entry.title)
            for entry in index.values()
            if entry.words
            and not (acronyms and entry.acronym and entry.acronym.lower() not in acronyms)
        ),
        reverse=True,
    )
    if not scored or scored[0][0] < MIN_WORD_MATCH:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None  # two titles fit equally well
    return scored[0][1]


def _find_match(
    index: dict[str, _CoreTitle], raw_name: str, venue_type: str | None = None
) -> tuple[str, str] | None:
    """Return (ranked title, method), or None.

    Journals skip the word match, because CORE ranks conferences and journal
    names such as "Pattern Recognition" share words with conference titles.
    """
    variants = _name_variants(raw_name)
    for name in variants:
        key = _clean_name(name)
        if key in index:
            return index[key].title, "title"

    for name in variants:
        key = _clean_name(name)
        acronyms = {acronym.lower() for acronym in _acronym_candidates(name)}
        if not acronyms:
            continue
        matches = [
            (entry_key, entry.title)
            for entry_key, entry in index.items()
            if entry.acronym and entry.acronym.lower() in acronyms
        ]
        scored = sorted(
            ((_word_overlap(key, entry_key), title) for entry_key, title in matches),
            reverse=True,
        )
        bare_acronym = bool(_ACRONYM.match(name.strip()))
        if scored and (
            scored[0][0] >= MIN_ACRONYM_WORD_OVERLAP
            or (bare_acronym and len(scored) == 1)
        ):
            return scored[0][1], "acronym"

    for name in variants if venue_type != "journal" else []:
        acronyms = {acronym.lower() for acronym in _acronym_candidates(name)}
        title = _closest_title(index, _clean_name(name), acronyms)
        if title:
            return title, "words"
    return None


def match_venue(
    conn: sqlite3.Connection,
    raw_name: str,
    venue_type: str | None = None,
    index: dict[str, _CoreTitle] | None = None,
) -> str | None:
    """Match by exact title, acronym, then distinctive words, and store the result.

    Pass `index` when matching many venues, so the ranked titles are read once.
    """
    row = conn.execute(
        "SELECT title FROM venue_alias WHERE raw_name = ?", (raw_name,)
    ).fetchone()
    if row:
        return row["title"]
    if venue_type == "repository":
        return None
    index = _titles_by_key(conn) if index is None else index
    if not index:
        return None
    match = _find_match(index, raw_name, venue_type)
    title, method = match if match else (None, "unmatched")
    conn.execute(
        "INSERT OR REPLACE INTO venue_alias(raw_name, title, method) VALUES (?, ?, ?)",
        (raw_name, title, method),
    )
    return title


def set_venue_alias(conn: sqlite3.Connection, raw_name: str, title: str | None) -> None:
    """Record a manual match. Pass None to mark a venue as having no rank."""
    if title is not None:
        row = conn.execute(
            "SELECT 1 FROM venue_rank WHERE title = ?", (title,)
        ).fetchone()
        if row is None:
            raise NotFound(f"No ranked venue titled: {title}")
    conn.execute(
        "INSERT OR REPLACE INTO venue_alias(raw_name, title, method) "
        "VALUES (?, ?, 'manual')",
        (raw_name, title),
    )
    conn.commit()


def _edition_year(edition: str) -> int:
    digits = re.search(r"\d{4}", edition)
    return int(digits.group(0)) if digits else 0


def rank_for(
    conn: sqlite3.Connection,
    raw_name: str | None,
    venue_type: str | None = None,
    edition: str | None = None,
) -> VenueRank | None:
    """Return a venue's rank: your target list first, then the newest CORE edition
    unless one is given."""
    if not raw_name:
        return None
    if edition is None:
        target = _target_rank(conn, _target_index(conn), raw_name, venue_type)
        if target:
            return target
    title = match_venue(conn, raw_name, venue_type)
    if title is None:
        return None
    rows = conn.execute(
        "SELECT edition, rank FROM venue_rank WHERE title = ?", (title,)
    ).fetchall()
    if edition:
        rows = [row for row in rows if row["edition"] == edition]
    if not rows:
        return None
    newest = max(rows, key=lambda row: _edition_year(row["edition"]))
    return VenueRank(newest["rank"], newest["edition"])


def _target_index(conn: sqlite3.Connection) -> dict[str, _CoreTitle]:
    rows = conn.execute("SELECT title, acronym FROM venue_target")
    index = {}
    for row in rows:
        key = _clean_name(row["title"])
        index[key] = _CoreTitle(row["title"], row["acronym"], _distinctive(key))
    return index


def _target_rank(
    conn: sqlite3.Connection, index: dict[str, _CoreTitle], raw_name: str, venue_type: str | None
) -> VenueRank | None:
    """Return the rank from your target list, if this venue is on it."""
    if not index or venue_type == "repository":
        return None
    match = _find_match(index, raw_name, venue_type)
    if match is None:
        return None
    row = conn.execute("SELECT rank FROM venue_target WHERE title = ?", (match[0],)).fetchone()
    return VenueRank(row["rank"], TARGET_EDITION)


def load_targets(path: Path) -> list[dict]:
    """Read a target venue list: `venues:` entries with a name, a rank and an optional acronym."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    targets = []
    for item in data.get("venues") or []:
        name = str(item.get("name") or "").strip()
        rank = str(item.get("rank") or "").strip()
        if not name or rank not in RANK_ORDER:
            raise BadInput(f"Each venue in {path.name} needs a name and a rank of A*, A, B or C.")
        acronym = str(item["acronym"]).strip() if item.get("acronym") else None
        targets.append({"title": name, "acronym": acronym, "rank": rank})
    return targets


def set_targets(conn: sqlite3.Connection, targets: list[dict]) -> int:
    """Replace your target venue list. Return its length."""
    conn.execute("DELETE FROM venue_target")
    for target in targets:
        add_target(conn, target["title"], target.get("acronym"), target["rank"], commit=False)
    conn.commit()
    return len(targets)


def add_target(
    conn: sqlite3.Connection,
    title: str,
    acronym: str | None,
    rank: str,
    commit: bool = True,
) -> None:
    """Add or update one target venue."""
    if not title.strip() or rank not in RANK_ORDER:
        raise BadInput("A target venue needs a name and a rank of A*, A, B or C.")
    conn.execute(
        "INSERT OR REPLACE INTO venue_target(title, acronym, rank) VALUES (?, ?, ?)",
        (title.strip(), (acronym or "").strip() or None, rank),
    )
    if commit:
        conn.commit()


def remove_target(conn: sqlite3.Connection, title: str) -> None:
    if conn.execute("DELETE FROM venue_target WHERE title = ?", (title,)).rowcount == 0:
        raise NotFound(f"No target venue titled: {title}")
    conn.commit()


def list_targets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT title, acronym, rank FROM venue_target ORDER BY rank, title")
    return [dict(row) for row in rows]


def _newest_ranks(conn: sqlite3.Connection) -> dict[str, VenueRank]:
    newest: dict[str, VenueRank] = {}
    for row in conn.execute("SELECT edition, title, rank FROM venue_rank"):
        current = newest.get(row["title"])
        if current is None or _edition_year(row["edition"]) > _edition_year(current.edition):
            newest[row["title"]] = VenueRank(row["rank"], row["edition"])
    return newest


def ranks_for_works(
    conn: sqlite3.Connection, work_ids: list[str]
) -> dict[str, VenueRank]:
    """Return ranks for papers. Papers without a rank are left out.

    Your target list is checked first, then CORE.
    """
    newest = _newest_ranks(conn)
    targets = _target_index(conn)
    if not newest and not targets:
        return {}
    index: dict[str, _CoreTitle] | None = None
    titles: dict[tuple[str, str | None], str | None] = {}
    targeted: dict[tuple[str, str | None], VenueRank | None] = {}
    ranks: dict[str, VenueRank] = {}
    for chunk in chunks(work_ids, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, venue, venue_type FROM work "
            f"WHERE venue IS NOT NULL AND id IN ({marks})",
            chunk,
        )
        for row in rows:
            key = (row["venue"], row["venue_type"])
            if key not in targeted:
                targeted[key] = _target_rank(conn, targets, *key)
            if targeted[key]:
                ranks[row["id"]] = targeted[key]
                continue
            if key not in titles:
                if index is None:
                    index = _titles_by_key(conn)
                titles[key] = match_venue(conn, *key, index=index)
            if titles[key] in newest:
                ranks[row["id"]] = newest[titles[key]]
    conn.commit()
    return ranks


def meets_minimum(rank: VenueRank | None, minimum: str | None) -> bool:
    """Return True when a paper passes a minimum-rank filter."""
    if minimum is None:
        return True
    if rank is None or rank.rank not in RANK_ORDER:
        return False
    return RANK_ORDER.index(rank.rank) <= RANK_ORDER.index(minimum)


def unmatched_venues(conn: sqlite3.Connection, project_id: int) -> list[str]:
    """List project venues that still need manual review."""
    rows = conn.execute(
        """
        SELECT DISTINCT w.venue, w.venue_type FROM membership m
        JOIN work w ON w.id = m.work_id
        WHERE m.project_id = ? AND w.venue IS NOT NULL
        AND COALESCE(w.venue_type, '') != 'repository'
        ORDER BY w.venue
        """,
        (project_id,),
    ).fetchall()
    index = _titles_by_key(conn)
    targets = _target_index(conn)
    unmatched = [
        row["venue"]
        for row in rows
        if match_venue(conn, row["venue"], row["venue_type"], index=index) is None
        and _target_rank(conn, targets, row["venue"], row["venue_type"]) is None
    ]
    conn.commit()
    return unmatched


def find_venues(
    conn: sqlite3.Connection, client: OpenAlexClient, name: str
) -> list[VenueCandidate]:
    """Find OpenAlex venues. A CORE acronym is expanded to its full title first."""
    row = conn.execute(
        "SELECT title FROM venue_rank WHERE acronym = ? COLLATE NOCASE LIMIT 1",
        (name.strip(),),
    ).fetchone()
    return client.search_sources(row["title"] if row else name)


def add_venue_papers(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    venue_id: str,
    since_year: int,
    cap: int = 200,
) -> int:
    """Add a venue's most cited recent papers to the frontier. Return the number added."""
    added = 0
    for record in client.works_by_source(venue_id, since_year, cap):
        work_id = upsert_work(conn, record)
        added += add_member(conn, project_id, work_id, "frontier")
    log_event(
        conn,
        project_id,
        "venue",
        {"venue_id": venue_id, "since_year": since_year, "added": added},
    )
    conn.commit()
    return added
