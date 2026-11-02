"""On-demand IEEE Xplore enrichment. IEEE supplies no citation edges.

Design rules, based on the IEEE API terms. Confirm with your library how they
apply to your key.
    1. Call IEEE only from a user action. No cron jobs or crawl-time calls.
    2. Store IEEE content only in ieee.db, so deleting that file purges it.
    3. Never send IEEE content to embeddings or an LLM.
    4. Keep IEEE fields out of exports and shared views.
    5. Limit to 10 calls per second, with a soft stop at 180 calls per day.
    6. Look up IEEE-hosted DOIs only, starting with the 10.1109 prefix.

Never import this module from rank.py or any future agent code.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from ariadnesearch.db import add_usage, chunks, connect, usage_today, utc_now
from ariadnesearch.errors import BudgetExceeded, MissingKey, UpstreamError
from ariadnesearch.records import normalize_doi

BASE_URL = "https://ieeexploreapi.ieee.org/api/v1"
MAX_DOIS_PER_CALL = 25
MAX_RECORDS_PER_CALL = 200
IEEE_DOI_PREFIXES = ("10.1109/",)

IEEE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ieee_record (
    doi                 TEXT PRIMARY KEY,
    article_number      TEXT,
    citing_paper_count  INTEGER,
    citing_patent_count INTEGER,
    ieee_terms_json     TEXT,
    abstract            TEXT,
    fetched_at          TEXT NOT NULL
);
"""


@dataclass
class IEEERecord:
    doi: str
    article_number: str | None = None
    title: str | None = None
    citing_paper_count: int | None = None
    citing_patent_count: int | None = None
    ieee_terms: list[str] = field(default_factory=list)
    abstract: str | None = None


def open_store(path: Path) -> sqlite3.Connection:
    """Open ieee.db and apply IEEE_SCHEMA."""
    conn = connect(path)
    conn.executescript(IEEE_SCHEMA)
    conn.commit()
    return conn


def is_ieee_doi(doi: str) -> bool:
    """Return True for DOIs worth sending to IEEE."""
    return normalize_doi(doi).startswith(IEEE_DOI_PREFIXES)


def _terms(article: dict) -> list[str]:
    """Read IEEE terms from either published response shape."""
    nested = ((article.get("index_terms") or {}).get("ieee_terms") or {}).get("terms")
    return list(nested or article.get("ieee_terms") or [])


def parse_article(article: dict) -> IEEERecord | None:
    """Convert one API article. Articles without a DOI are skipped."""
    if not article.get("doi"):
        return None
    number = article.get("article_number")
    return IEEERecord(
        doi=normalize_doi(article["doi"]),
        article_number=str(number) if number is not None else None,
        title=article.get("title"),
        citing_paper_count=article.get("citing_paper_count"),
        citing_patent_count=article.get("citing_patent_count"),
        ieee_terms=_terms(article),
        abstract=article.get("abstract"),
    )


class IEEEClient:
    def __init__(
        self,
        api_key: str | None,
        store: sqlite3.Connection,
        usage_conn: sqlite3.Connection,
        daily_soft_limit: int = 180,
        rate_per_second: int = 10,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key
        self.store = store
        self.usage_conn = usage_conn  # graph.db, for call counts only
        self.daily_soft_limit = daily_soft_limit
        self.min_interval = 1 / rate_per_second
        self.http = http or httpx.Client(base_url=BASE_URL, timeout=30)
        self.sleep = sleep
        self._last_call = 0.0

    def calls_today(self) -> int:
        return usage_today(self.usage_conn, "ieee")[0]

    def _request(self, path: str, params: dict) -> dict:
        if not self.api_key:
            raise MissingKey("IEEE_API_KEY is not set in `.env`.")
        used = self.calls_today()
        if used >= self.daily_soft_limit:
            raise BudgetExceeded(
                f"IEEE soft limit reached ({used} of {self.daily_soft_limit} calls today)."
            )
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self.sleep(wait)
        self._last_call = time.monotonic()

        # A failed call may still count against the IEEE quota, so count it first.
        add_usage(self.usage_conn, "ieee", 1)
        self.usage_conn.commit()
        try:
            response = self.http.get(
                path, params={**params, "apikey": self.api_key, "format": "json"}
            )
        except httpx.TransportError:
            raise UpstreamError("Could not reach IEEE Xplore.") from None
        if response.status_code in (401, 403):
            raise UpstreamError("IEEE rejected the API key, or the quota is used up.")
        if response.status_code >= 400:
            raise UpstreamError(f"IEEE returned HTTP {response.status_code}.")
        return response.json()

    def _save(self, records: list[IEEERecord]) -> None:
        now = utc_now()
        self.store.executemany(
            """
            INSERT OR REPLACE INTO ieee_record(
                doi, article_number, citing_paper_count, citing_patent_count,
                ieee_terms_json, abstract, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.doi,
                    record.article_number,
                    record.citing_paper_count,
                    record.citing_patent_count,
                    json.dumps(record.ieee_terms),
                    record.abstract,
                    now,
                )
                for record in records
            ],
        )
        self.store.commit()

    def cached(self, doi: str) -> IEEERecord | None:
        """Return a stored record. This makes no API call."""
        row = self.store.execute(
            "SELECT * FROM ieee_record WHERE doi = ?", (normalize_doi(doi),)
        ).fetchone()
        if row is None:
            return None
        return IEEERecord(
            doi=row["doi"],
            article_number=row["article_number"],
            citing_paper_count=row["citing_paper_count"],
            citing_patent_count=row["citing_patent_count"],
            ieee_terms=json.loads(row["ieee_terms_json"] or "[]"),
            abstract=row["abstract"],
        )

    def lookup_dois(self, dois: list[str], refresh: bool = False) -> list[IEEERecord]:
        """Fetch through `/articles/doi/{dois}`, up to 25 per call.

        Only IEEE-hosted DOIs missing from the store are requested, unless
        `refresh` is set.
        """
        wanted = sorted({normalize_doi(doi) for doi in dois if is_ieee_doi(doi)})
        records = [] if refresh else [
            record for doi in wanted if (record := self.cached(doi))
        ]
        missing = wanted if refresh else [
            doi for doi in wanted if self.cached(doi) is None
        ]
        for chunk in chunks(missing, MAX_DOIS_PER_CALL):
            path = "/articles/doi/" + quote(",".join(chunk), safe="/,()")
            body = self._request(path, {})
            parsed = [parse_article(item) for item in body.get("articles") or []]
            found = [record for record in parsed if record]
            self._save(found)
            records.extend(found)
        return records

    def search(self, querytext: str, max_records: int = 25) -> list[IEEERecord]:
        """Search `/search/articles`. Call this only from a user action."""
        body = self._request(
            "/search/articles",
            {
                "querytext": querytext,
                "max_records": min(max_records, MAX_RECORDS_PER_CALL),
            },
        )
        parsed = [parse_article(item) for item in body.get("articles") or []]
        found = [record for record in parsed if record]
        self._save(found)
        return found
