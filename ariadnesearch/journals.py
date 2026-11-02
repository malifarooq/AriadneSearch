"""Journal impact from OpenAlex, shown next to journal papers.

OpenAlex's 2-year mean citedness uses the impact factor formula: citations in one
year to a journal's papers from the two years before, divided by the number of
those papers. Clarivate's Journal Impact Factor is licensed, has no public API and
counts citations from its own index, so the two numbers differ.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ariadnesearch.db import chunks, utc_now
from ariadnesearch.openalex import OpenAlexClient

STATS_MAX_AGE = timedelta(days=90)  # the figures change once a year


@dataclass
class JournalImpact:
    mean_citedness: float | None
    h_index: int | None


def _journals(conn: sqlite3.Connection, work_ids: Iterable[str]) -> dict[str, str]:
    """Map journal papers to their OpenAlex source IDs."""
    found: dict[str, str] = {}
    for chunk in chunks(sorted(set(work_ids)), 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, venue_id FROM work WHERE venue_type = 'journal' "
            f"AND venue_id IS NOT NULL AND id IN ({marks})",
            chunk,
        )
        found.update({row["id"]: row["venue_id"] for row in rows})
    return found


def refresh_impact(
    conn: sqlite3.Connection, client: OpenAlexClient, work_ids: Iterable[str]
) -> int:
    """Look up these papers' journals when missing or stale. Return the number fetched.

    This costs one list call per 100 journals.
    """
    venues = sorted(set(_journals(conn, work_ids).values()))
    cutoff = (datetime.now(timezone.utc) - STATS_MAX_AGE).isoformat(timespec="seconds")
    fresh: set[str] = set()
    for chunk in chunks(venues, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT venue_id FROM venue_stats WHERE fetched_at >= ? "
            f"AND venue_id IN ({marks})",
            (cutoff, *chunk),
        )
        fresh.update(row["venue_id"] for row in rows)
    stats = client.sources_by_ids([venue for venue in venues if venue not in fresh])
    now = utc_now()
    conn.executemany(
        "INSERT OR REPLACE INTO venue_stats(venue_id, mean_citedness, h_index, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        [(item.id, item.mean_citedness, item.h_index, now) for item in stats],
    )
    conn.commit()
    return len(stats)


def impact_for_works(
    conn: sqlite3.Connection, work_ids: Iterable[str]
) -> dict[str, JournalImpact]:
    """Return impact for journal papers already looked up. Others are left out."""
    journals = _journals(conn, work_ids)
    stats: dict[str, JournalImpact] = {}
    for chunk in chunks(sorted(set(journals.values())), 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT venue_id, mean_citedness, h_index FROM venue_stats "
            f"WHERE venue_id IN ({marks})",
            chunk,
        )
        stats.update(
            {row["venue_id"]: JournalImpact(row["mean_citedness"], row["h_index"]) for row in rows}
        )
    return {work_id: stats[venue] for work_id, venue in journals.items() if venue in stats}
