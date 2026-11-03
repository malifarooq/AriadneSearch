"""Remove stored papers that can never become suggestions.

Most stored papers are placeholders: papers known only because a fetched paper
cites them. A suggestion must cite or be cited by one of a project's papers, or
share at least two citing papers or references with one. So a placeholder cited
by a single paper that is in no project can never be suggested, and it is deleted
with its link. If that citing paper joins a project later, Update graph fetches
its record again, one list call per 100 papers (see `expand.hydrate`), so
nothing is lost for good.
Placeholders a project, a citation list, a library or a relation refers to stay.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CACHE_MAX_AGE = timedelta(days=30)  # OpenAlexClient ignores older entries anyway


@dataclass
class PruneReport:
    papers: int = 0
    links: int = 0
    cache_entries: int = 0


def prune(
    conn: sqlite3.Connection, vacuum: bool = False, cache_max_age: timedelta = CACHE_MAX_AGE
) -> PruneReport:
    """Delete unreachable placeholders and expired cache entries.

    With `vacuum`, also shrink both database files, which takes a while on a
    large database and needs as much free disk as the file's size.
    """
    conn.commit()
    conn.executescript(
        """
        DROP TABLE IF EXISTS temp.keep;
        DROP TABLE IF EXISTS temp.prunable;
        CREATE TEMP TABLE keep (id INTEGER PRIMARY KEY);
        CREATE TEMP TABLE prunable (id INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO temp.keep
            SELECT work_id FROM membership
            UNION SELECT work_id FROM my_citation
            UNION SELECT work_id FROM library_item
            UNION SELECT work_id FROM ref_import
            UNION SELECT a FROM relation
            UNION SELECT b FROM relation;
        INSERT INTO temp.prunable
            SELECT w.id FROM work w
            LEFT JOIN (
                SELECT dst, COUNT(*) AS n, MAX(src) AS citer FROM cites GROUP BY dst
            ) cited ON cited.dst = w.id
            WHERE w.level = 'stub'
            AND w.id NOT IN temp.keep
            AND COALESCE(cited.n, 0) <= 1
            AND (cited.citer IS NULL OR cited.citer NOT IN temp.keep)
            AND NOT EXISTS (SELECT 1 FROM cites own WHERE own.src = w.id);
        """
    )
    report = PruneReport()
    report.links = conn.execute("DELETE FROM cites WHERE dst IN temp.prunable").rowcount
    conn.execute("DELETE FROM work_alias WHERE work_id IN temp.prunable")
    conn.execute("DELETE FROM fetch_state WHERE work_id IN temp.prunable")
    report.papers = conn.execute("DELETE FROM work WHERE id IN temp.prunable").rowcount
    cutoff = (datetime.now(timezone.utc) - cache_max_age).isoformat(timespec="seconds")
    report.cache_entries = conn.execute(
        "DELETE FROM cache.raw_response WHERE fetched_at < ?", (cutoff,)
    ).rowcount
    conn.commit()
    conn.executescript("DROP TABLE temp.keep; DROP TABLE temp.prunable;")
    if vacuum:
        conn.execute("VACUUM")
        conn.execute("VACUUM cache")
    return report
