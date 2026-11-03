"""Decide when two records are the same paper, and merge them reversibly.

A preprint and its published version become one node. A conference paper and
its journal extension stay as two nodes, linked through `relation`.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from typing import Literal

from ariadnesearch.db import chunks, utc_now
from ariadnesearch.errors import BadInput, NotFound
from ariadnesearch.records import (
    WorkRecord,
    arxiv_doi,
    normalize_doi,
    normalize_title,
)

__all__ = [
    "arxiv_doi",
    "best_title_match",
    "classify_pair",
    "merge_works",
    "normalize_doi",
    "normalize_title",
    "resolve",
    "resolve_many",
    "store_references",
    "undo_merge",
    "upsert_stubs",
    "upsert_work",
]

PairKind = Literal["same", "extension", "different"]

MAX_VERSION_YEAR_GAP = 3
MIN_TITLE_WORDS = 3  # short titles such as "Editorial" repeat across papers
MIN_TITLE_MATCH = 0.9
_LEVELS = {"stub": 0, "hydrated": 1, "expanded": 2}
_STATE_STRENGTH = {"frontier": 0, "rejected": 1, "read": 2, "accepted": 3, "seed": 4}

# (table, column pointing at a work, primary key columns)
_MOVED_ROWS = [
    ("cites", "src", ("src", "dst")),
    ("cites", "dst", ("src", "dst")),
    ("relation", "a", ("a", "b", "kind")),
    ("relation", "b", ("a", "b", "kind")),
    ("membership", "work_id", ("project_id", "work_id")),
    ("populated", "work_id", ("project_id", "work_id")),
    ("my_citation", "work_id", ("my_paper_id", "work_id", "source")),
    ("library_item", "work_id", ("work_id", "source")),
    ("fetch_state", "work_id", ("work_id",)),
    ("ref_import", "work_id", ("work_id",)),
]


def resolve(conn: sqlite3.Connection, scheme: str, value: str) -> int | None:
    """Return the internal work ID for an external identifier."""
    row = conn.execute(
        "SELECT work_id FROM work_alias WHERE scheme = ? AND value = ?",
        (scheme, value),
    ).fetchone()
    return row["work_id"] if row else None


def resolve_many(
    conn: sqlite3.Connection, scheme: str, values: list[str]
) -> dict[str, int]:
    """Return {identifier: internal ID} for the identifiers already known."""
    found: dict[str, int] = {}
    for chunk in chunks(values, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT value, work_id FROM work_alias "
            f"WHERE scheme = ? AND value IN ({marks})",
            (scheme, *chunk),
        )
        found.update({row["value"]: row["work_id"] for row in rows})
    return found


def add_alias(conn: sqlite3.Connection, scheme: str, value: str, work_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO work_alias(scheme, value, work_id) VALUES (?, ?, ?)",
        (scheme, value, work_id),
    )


def upsert_stubs(
    conn: sqlite3.Connection, openalex_ids: list[str]
) -> dict[str, int]:
    """Record papers known only from a reference list. This costs no API call."""
    unique = list(dict.fromkeys(openalex_ids))
    known = resolve_many(conn, "openalex", unique)
    missing = [item for item in unique if item not in known]
    if not missing:
        return known
    # Reserve a block of IDs. The write transaction keeps other writers out
    # until it commits, so no one else can take the same block.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    start = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM work").fetchone()[0]
    new = {item: start + offset for offset, item in enumerate(missing)}
    conn.executemany(
        "INSERT INTO work(id, level) VALUES (?, 'stub')",
        [(work_id,) for work_id in new.values()],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO work_alias(scheme, value, work_id) "
        "VALUES ('openalex', ?, ?)",
        list(new.items()),
    )
    return {**known, **new}


def store_references(
    conn: sqlite3.Connection, work_id: int, openalex_ids: list[str]
) -> None:
    """Store a paper's outgoing citation edges."""
    targets = upsert_stubs(conn, openalex_ids)
    conn.executemany(
        "INSERT OR IGNORE INTO cites(src, dst) VALUES (?, ?)",
        [(work_id, dst) for dst in set(targets.values()) if dst != work_id],
    )


def _aliases(record: WorkRecord) -> list[tuple[str, str]]:
    pairs = [("openalex", record.openalex_id)]
    if record.doi:
        pairs.append(("doi", normalize_doi(record.doi)))
    if record.arxiv_id:
        pairs.append(("arxiv", record.arxiv_id))
        pairs.append(("doi", arxiv_doi(record.arxiv_id)))
    return pairs


def _row_to_record(row: sqlite3.Row) -> WorkRecord:
    """Build a comparison record. `openalex_id` holds the internal ID here."""
    return WorkRecord(
        openalex_id=row["id"],
        title=row["title"],
        year=row["year"],
        type=row["type"],
        venue=row["venue"],
        venue_type=row["venue_type"],
        authors=json.loads(row["authors"] or "[]"),
        cited_by_count=row["oa_cited_by"],
    )


def upsert_work(conn: sqlite3.Connection, record: WorkRecord) -> int:
    """Insert or update a work and its references, then return its internal ID.

    Records sharing an identifier are merged. Other versions of the same paper
    are found by title.
    """
    aliases = _aliases(record)
    matches = list(
        dict.fromkeys(
            found
            for found in (resolve(conn, scheme, value) for scheme, value in aliases)
            if found
        )
    )
    if matches:
        work_id = matches[0]
        for other in matches[1:]:
            merge_works(conn, work_id, other, rule="shared-id", confidence=1.0)
    else:
        work_id = conn.execute("INSERT INTO work(level) VALUES ('stub')").lastrowid

    conn.execute(
        """
        UPDATE work SET
            title = COALESCE(?, title),
            title_key = COALESCE(?, title_key),
            authors = COALESCE(?, authors),
            year = COALESCE(?, year),
            pub_date = COALESCE(?, pub_date),
            type = COALESCE(?, type),
            venue = COALESCE(?, venue),
            venue_id = COALESCE(?, venue_id),
            venue_type = COALESCE(?, venue_type),
            abstract = COALESCE(?, abstract),
            oa_cited_by = COALESCE(?, oa_cited_by),
            oa_pct = COALESCE(?, oa_pct),
            n_refs = MAX(COALESCE(n_refs, 0), ?),
            topic = COALESCE(?, topic),
            subfield = COALESCE(?, subfield),
            field = COALESCE(?, field),
            level = CASE WHEN level = 'stub' THEN 'hydrated' ELSE level END
        WHERE id = ?
        """,
        (
            record.title,
            normalize_title(record.title) if record.title else None,
            json.dumps(record.authors) if record.authors else None,
            record.year,
            record.pub_date,
            record.type,
            record.venue,
            record.venue_id,
            record.venue_type,
            record.abstract,
            record.cited_by_count,
            record.percentile,
            record.n_refs,
            record.topic,
            record.subfield,
            record.field,
            work_id,
        ),
    )
    for scheme, value in aliases:
        add_alias(conn, scheme, value, work_id)
    store_references(conn, work_id, record.referenced_ids)
    return _reconcile_versions(conn, work_id)


def _is_preprint(record: WorkRecord) -> bool:
    return record.type == "preprint" or record.venue_type == "repository"


def _reconcile_versions(conn: sqlite3.Connection, work_id: int) -> int:
    """Merge other versions of this paper, and link journal extensions."""
    row = conn.execute("SELECT * FROM work WHERE id = ?", (work_id,)).fetchone()
    if not row["title_key"]:
        return work_id
    current = _row_to_record(row)
    others = conn.execute(
        "SELECT * FROM work WHERE title_key = ? AND id != ?",
        (row["title_key"], work_id),
    ).fetchall()
    for other_row in others:
        other = _row_to_record(other_row)
        kind = classify_pair(current, other)
        if kind == "same":
            keep, drop = _preferred(current, other)
            merge_works(conn, keep, drop, rule="same-title", confidence=0.9)
            work_id = keep
            kept_row = conn.execute(
                "SELECT * FROM work WHERE id = ?", (keep,)
            ).fetchone()
            current = _row_to_record(kept_row)
        elif kind == "extension":
            first, second = sorted(
                (current, other), key=lambda item: item.year or 0
            )
            conn.execute(
                "INSERT OR IGNORE INTO relation(a, b, kind) VALUES (?, ?, 'extended_by')",
                (first.openalex_id, second.openalex_id),
            )
    return work_id


def _preferred(a: WorkRecord, b: WorkRecord) -> tuple[str, str]:
    """Return (keep, drop), favouring the published version."""
    ordered = sorted(
        (a, b), key=lambda item: (_is_preprint(item), -(item.cited_by_count or 0))
    )
    return ordered[0].openalex_id, ordered[1].openalex_id


def _last_names(record: WorkRecord) -> set[str]:
    return {name.split()[-1].lower() for name in record.authors if name.split()}


def classify_pair(a: WorkRecord, b: WorkRecord) -> PairKind:
    """Classify two records as the same paper, an extension, or unrelated."""
    if not a.title or not b.title:
        return "different"
    title = normalize_title(a.title)
    if title != normalize_title(b.title) or len(title.split()) < MIN_TITLE_WORDS:
        return "different"
    if a.year and b.year and abs(a.year - b.year) > MAX_VERSION_YEAR_GAP:
        return "different"
    names_a, names_b = _last_names(a), _last_names(b)
    if names_a and names_b and not names_a & names_b:
        return "different"
    if _is_preprint(a) or _is_preprint(b):
        return "same"
    if a.venue and b.venue and a.venue != b.venue:
        return "extension"
    return "same"


def best_title_match(
    records: list[WorkRecord], title: str
) -> WorkRecord | None:
    """Return the record whose title closely matches, or None."""
    wanted = normalize_title(title)
    best: WorkRecord | None = None
    best_score = 0.0
    for record in records:
        if not record.title:
            continue
        score = difflib.SequenceMatcher(
            None, wanted, normalize_title(record.title)
        ).ratio()
        if score > best_score:
            best, best_score = record, score
    return best if best_score >= MIN_TITLE_MATCH else None


def _insert_row(conn: sqlite3.Connection, table: str, row: dict):
    columns = ",".join(row)
    marks = ",".join("?" * len(row))
    return conn.execute(
        f"INSERT OR IGNORE INTO {table}({columns}) VALUES ({marks})",
        list(row.values()),
    )


def _delete_row(
    conn: sqlite3.Connection, table: str, keys: tuple[str, ...], row: dict
) -> None:
    where = " AND ".join(f"{key} = ?" for key in keys)
    conn.execute(f"DELETE FROM {table} WHERE {where}", [row[key] for key in keys])


def _move_rows(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    keys: tuple[str, ...],
    keep_id: str,
    drop_id: str,
) -> dict:
    rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE {column} = ?", (drop_id,)
        )
    ]
    added = []
    for row in rows:
        moved = {**row, column: keep_id}
        if table in ("cites", "relation") and len({moved[key] for key in keys[:2]}) == 1:
            continue
        if _insert_row(conn, table, moved).rowcount:
            added.append({key: moved[key] for key in keys})
    conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (drop_id,))
    return {"table": table, "keys": list(keys), "rows": rows, "added": added}


def merge_works(
    conn: sqlite3.Connection,
    keep_id: str,
    drop_id: str,
    rule: str,
    confidence: float,
) -> int:
    """Move aliases, edges and memberships to `keep_id`, then delete `drop_id`.

    Everything needed for undo is saved in `merge_log.snapshot_json`. Return the
    merge ID.
    """
    if keep_id == drop_id:
        raise BadInput("Cannot merge a work with itself.")
    dropped = dict(
        conn.execute("SELECT * FROM work WHERE id = ?", (drop_id,)).fetchone()
    )
    aliases = [
        dict(row)
        for row in conn.execute(
            "SELECT scheme, value FROM work_alias WHERE work_id = ?", (drop_id,)
        )
    ]
    kept_states = {
        row["project_id"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM membership WHERE work_id = ?", (keep_id,)
        )
    }
    dropped_states = conn.execute(
        "SELECT project_id, state FROM membership WHERE work_id = ?", (drop_id,)
    ).fetchall()

    conn.execute(
        "UPDATE work_alias SET work_id = ? WHERE work_id = ?", (keep_id, drop_id)
    )
    moves = [
        _move_rows(conn, table, column, keys, keep_id, drop_id)
        for table, column, keys in _MOVED_ROWS
    ]

    overwritten = []
    for row in dropped_states:
        kept = kept_states.get(row["project_id"])
        if kept and _STATE_STRENGTH[row["state"]] > _STATE_STRENGTH[kept["state"]]:
            overwritten.append(kept)
            conn.execute(
                "UPDATE membership SET state = ? WHERE project_id = ? AND work_id = ?",
                (row["state"], row["project_id"], keep_id),
            )

    conn.execute(
        """
        UPDATE work SET
            abstract = COALESCE(abstract, ?),
            n_refs = CASE WHEN COALESCE(n_refs, 0) = 0 THEN ? ELSE n_refs END,
            oa_cited_by = MAX(COALESCE(oa_cited_by, 0), COALESCE(?, 0)),
            level = CASE WHEN ? = 'expanded' THEN 'expanded' ELSE level END
        WHERE id = ?
        """,
        (
            dropped["abstract"],
            dropped["n_refs"],
            dropped["oa_cited_by"],
            dropped["level"],
            keep_id,
        ),
    )
    conn.execute("DELETE FROM work WHERE id = ?", (drop_id,))

    snapshot = {
        "work": dropped,
        "aliases": aliases,
        "moves": moves,
        "overwritten_states": overwritten,
    }
    cursor = conn.execute(
        "INSERT INTO merge_log(keep_id, drop_id, rule, confidence, snapshot_json, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (keep_id, drop_id, rule, confidence, json.dumps(snapshot), utc_now()),
    )
    return int(cursor.lastrowid)


def undo_merge(conn: sqlite3.Connection, merge_id: int) -> None:
    """Restore the dropped work from its merge snapshot."""
    log = conn.execute(
        "SELECT * FROM merge_log WHERE id = ? AND undone_at IS NULL", (merge_id,)
    ).fetchone()
    if log is None:
        raise NotFound(f"No active merge with ID {merge_id}.")
    snapshot = json.loads(log["snapshot_json"])

    _insert_row(conn, "work", snapshot["work"])
    for alias in snapshot["aliases"]:
        conn.execute(
            "UPDATE work_alias SET work_id = ? WHERE scheme = ? AND value = ?",
            (log["drop_id"], alias["scheme"], alias["value"]),
        )
    for move in snapshot["moves"]:
        for row in move["added"]:
            _delete_row(conn, move["table"], tuple(move["keys"]), row)
    for move in snapshot["moves"]:
        for row in move["rows"]:
            _insert_row(conn, move["table"], row)
    for kept in snapshot["overwritten_states"]:
        conn.execute(
            "UPDATE membership SET state = ? WHERE project_id = ? AND work_id = ?",
            (kept["state"], kept["project_id"], kept["work_id"]),
        )
    conn.execute(
        "UPDATE merge_log SET undone_at = ? WHERE id = ?", (utc_now(), merge_id)
    )
