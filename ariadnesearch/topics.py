"""OpenAlex topics, used to colour the graph by research area.

Papers get their topic when they are fetched. Papers stored before topics were
kept are looked up in batches of 100 the next time their project is updated.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from ariadnesearch.db import chunks
from ariadnesearch.openalex import OpenAlexClient


def refresh_topics(
    conn: sqlite3.Connection, client: OpenAlexClient, work_ids: Iterable[str]
) -> int:
    """Look up topics for papers that have never had one checked. Return how many."""
    by_openalex: dict[str, str] = {}
    for chunk in chunks(sorted(set(work_ids)), 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT a.value, w.id FROM work w
            JOIN work_alias a ON a.work_id = w.id AND a.scheme = 'openalex'
            WHERE w.topic IS NULL AND w.id IN ({marks})
            """,
            chunk,
        )
        by_openalex.update({row[0]: row[1] for row in rows})
    if not by_openalex:
        return 0
    topics = client.topics_by_ids(list(by_openalex))
    updates = []
    for openalex_id, work_id in by_openalex.items():
        topic = topics.get(openalex_id) or {}
        updates.append(
            (
                topic.get("display_name") or "",  # '' records that it has none
                (topic.get("subfield") or {}).get("display_name"),
                (topic.get("field") or {}).get("display_name"),
                work_id,
            )
        )
    conn.executemany(
        "UPDATE work SET topic = ?, subfield = ?, field = ? WHERE id = ? AND topic IS NULL",
        updates,
    )
    conn.commit()
    return len(set(by_openalex.values()))
