"""SQLite connection, setup and small shared write helpers."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from ariadnesearch.errors import BadInput, NotFound

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
CACHE_SCHEMA_PATH = Path(__file__).with_name("cache_schema.sql")
SCHEMA_VERSION = 2  # stored in PRAGMA user_version; 0 means the version 1 layout
T = TypeVar("T")
log = logging.getLogger("ariadnesearch")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def chunks(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def cache_path(path: Path) -> Path:
    """Return the cache database that sits beside a graph database."""
    return path.with_name("cache.db")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each request or command owns one connection, and uses it serially.
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("ATTACH DATABASE ? AS cache", (str(cache_path(path)),))
    conn.execute("PRAGMA cache.journal_mode = WAL")
    return conn


def init_db(path: Path) -> sqlite3.Connection:
    """Create or upgrade the database. Safe to run more than once.

    A database in the version 1 layout is copied into the current one first, and
    the old file is kept beside it as a backup.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if _needs_upgrade(path):
        backup = _upgrade_from_v1(path)
        log.warning(
            "Upgraded %s to the version %d layout. The old file is kept as %s.",
            path.name, SCHEMA_VERSION, backup.name,
        )
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.executescript(CACHE_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def _needs_upgrade(path: Path) -> bool:
    if not path.exists():
        return False
    probe = sqlite3.connect(path)
    try:
        version = probe.execute("PRAGMA user_version").fetchone()[0]
        has_work = probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'work'"
        ).fetchone()
    finally:
        probe.close()
    return version < SCHEMA_VERSION and has_work is not None


# Version 1 columns that hold a work ID, by table. Text IDs become integers.
_WORK_ID_COLUMNS = {
    "work": ("id",),
    "work_alias": ("work_id",),
    "cites": ("src", "dst"),
    "relation": ("a", "b"),
    "membership": ("work_id",),
    "populated": ("work_id",),
    "ref_import": ("work_id",),
    "fetch_state": ("work_id",),
    "my_citation": ("work_id",),
    "library_item": ("work_id",),
}
_COPIED_AS_IS = (
    "project", "event", "budget", "my_paper",
    "venue_rank", "venue_stats", "venue_target", "venue_alias",
)


def _copy_table(conn: sqlite3.Connection, table: str, id_columns: tuple[str, ...] = ()) -> None:
    """Copy one table from the attached `old` database, mapping work IDs."""
    new_columns = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")]
    old_columns = {row[1] for row in conn.execute(f"PRAGMA old.table_info({table})")}
    selected, joins = [], []
    for column in new_columns:
        if column in id_columns:
            joins.append(f"JOIN temp.id_map m_{column} ON m_{column}.old = t.{column}")
            selected.append(f"m_{column}.new")
        elif table == "cites" and column == "source":
            selected.append("NULLIF(t.sources, 'openalex')" if "sources" in old_columns else "NULL")
        else:
            selected.append(f"t.{column}" if column in old_columns else "NULL")
    conn.execute(
        f"INSERT OR IGNORE INTO main.{table}({', '.join(new_columns)}) "
        f"SELECT {', '.join(selected)} FROM old.{table} t {' '.join(joins)}"
    )


def _upgrade_from_v1(path: Path) -> Path:
    """Copy a version 1 database into the current layout. Return the backup's path.

    Text work IDs become integers, citation edges drop their unused timestamp,
    and cached responses move to cache.db, leaving out paged lists that are never
    read again. Merges made before the upgrade cannot be undone afterwards, so
    merge_log stays in the backup only.
    """
    old = sqlite3.connect(path)
    try:
        busy = old.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    finally:
        old.close()
    if busy:
        raise BadInput(
            f"{path.name} is open in another AriadneSearch process. Stop it, then try again."
        )
    staging = path.with_name(path.name + ".upgrading")
    staging.unlink(missing_ok=True)
    conn = sqlite3.connect(staging)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute("ATTACH DATABASE ? AS old", (str(path),))
        conn.execute("ATTACH DATABASE ? AS cache", (str(cache_path(path)),))
        conn.executescript(CACHE_SCHEMA_PATH.read_text(encoding="utf-8"))
        tables = {
            row[0] for row in conn.execute("SELECT name FROM old.sqlite_master WHERE type = 'table'")
        }
        conn.execute("CREATE TEMP TABLE id_map (old TEXT PRIMARY KEY, new INTEGER NOT NULL)")
        conn.execute(
            "INSERT INTO temp.id_map SELECT id, ROW_NUMBER() OVER (ORDER BY rowid) FROM old.work"
        )
        for table, id_columns in _WORK_ID_COLUMNS.items():
            if table in tables:
                _copy_table(conn, table, id_columns)
        for table in _COPIED_AS_IS:
            if table in tables:
                _copy_table(conn, table)
        if "raw_response" in tables:
            conn.execute(
                "INSERT OR IGNORE INTO cache.raw_response "
                "SELECT key, url, body, fetched_at FROM old.raw_response "
                "WHERE url NOT LIKE '%cursor=%'"
            )
        conn.execute(f"PRAGMA main.user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    backup = path.with_name(f"{path.stem}.v1-backup{path.suffix}")
    if backup.exists():
        backup = path.with_name(f"{path.stem}.v1-backup-{utc_now()[:19].replace(':', '')}{path.suffix}")
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    path.replace(backup)
    staging.replace(path)
    return backup


def create_project(conn: sqlite3.Connection, name: str) -> int:
    """Insert a project and return its ID."""
    name = name.strip()
    if not name:
        raise BadInput("Project name cannot be empty.")
    try:
        cursor = conn.execute("INSERT INTO project(name) VALUES (?)", (name,))
    except sqlite3.IntegrityError as exc:
        raise BadInput(f"Project already exists: {name}") from exc
    conn.commit()
    return int(cursor.lastrowid)


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    """Remove a project and its decisions. The papers stay in the shared tables."""
    for table in ("membership", "populated", "event", "project_author"):
        conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
    conn.commit()


def is_author_project(conn: sqlite3.Connection, project_id: int) -> bool:
    """Return whether a project follows one author's papers. See author.py."""
    row = conn.execute(
        "SELECT 1 FROM project_author WHERE project_id = ? LIMIT 1", (project_id,)
    ).fetchone()
    return row is not None


def get_project_id(conn: sqlite3.Connection, name: str) -> int:
    """Return a project's ID, or raise NotFound if it does not exist."""
    row = conn.execute("SELECT id FROM project WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise NotFound(f"No project named: {name}")
    return int(row["id"])


def log_event(
    conn: sqlite3.Connection, project_id: int | None, event_type: str, payload: dict
) -> None:
    """Append one row to `event`."""
    conn.execute(
        "INSERT INTO event(project_id, ts, type, payload_json) VALUES (?, ?, ?, ?)",
        (project_id, utc_now(), event_type, json.dumps(payload)),
    )


def add_usage(
    conn: sqlite3.Connection, source: str, calls: int, cost_usd: float = 0.0
) -> None:
    """Add to today's `budget` row for a source."""
    conn.execute(
        """
        INSERT INTO budget(source, day, calls, cost_usd) VALUES (?, ?, ?, ?)
        ON CONFLICT(source, day) DO UPDATE SET
            calls = calls + excluded.calls,
            cost_usd = cost_usd + excluded.cost_usd
        """,
        (source, utc_day(), calls, cost_usd),
    )


def usage_today(conn: sqlite3.Connection, source: str) -> tuple[int, float]:
    """Return today's (calls, cost_usd) for a source."""
    row = conn.execute(
        "SELECT calls, cost_usd FROM budget WHERE source = ? AND day = ?",
        (source, utc_day()),
    ).fetchone()
    return (int(row["calls"]), float(row["cost_usd"])) if row else (0, 0.0)
