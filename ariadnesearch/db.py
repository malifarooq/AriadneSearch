"""SQLite connection, setup and small shared write helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from ariadnesearch.errors import BadInput, NotFound

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def chunks(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each request or command owns one connection, and uses it serially.
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# Columns added after the first release, for databases created before them.
ADDED_COLUMNS = {"work": ("topic TEXT", "subfield TEXT", "field TEXT")}


def init_db(path: Path) -> sqlite3.Connection:
    """Create any missing tables and columns. Safe to run more than once."""
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, columns in ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    conn.commit()
    return conn


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
    for table in ("membership", "populated", "event"):
        conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
    conn.commit()


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
