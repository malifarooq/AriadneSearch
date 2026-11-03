"""OpenAlex client. This is the only source of citation edges.

Costs and limits used here, checked November 2026:
    Single lookup: free. List or filter: $0.10 per 1,000 calls.
    Search: $1 per 1,000 calls. Free key: $1 per day.
    100 results per page. 100 values per OR filter.
    Cursor paging past the first page needs a key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, urlencode

import httpx

from ariadnesearch import __version__
from ariadnesearch.db import add_usage, chunks, usage_today, utc_now
from ariadnesearch.errors import BudgetExceeded, MissingKey, UpstreamError
from ariadnesearch.records import WorkRecord, clean_arxiv_id, normalize_doi

__all__ = [
    "OpenAlexClient",
    "SourceStats",
    "VenueCandidate",
    "WorkRecord",
    "decode_abstract",
    "parse_work",
]

logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_URL = "https://api.openalex.org"
MAX_PER_PAGE = 100
MAX_OR_VALUES = 100
MAX_DOIS_PER_CALL = 50  # keeps long DOI filters to a safe URL length
MAX_RETRIES = 6
SELECT_FIELDS = ",".join(
    [
        "id",
        "doi",
        "ids",
        "title",
        "publication_year",
        "publication_date",
        "type",
        "primary_location",
        "locations",
        "authorships",
        "abstract_inverted_index",
        "cited_by_count",
        "citation_normalized_percentile",
        "referenced_works",
        "referenced_works_count",
        "primary_topic",
    ]
)
CiterOrder = Literal["newest", "most_cited"]
CITER_SORTS = {"newest": "publication_date:desc", "most_cited": "cited_by_count:desc"}
_ARXIV_URL = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z.\-]+/[0-9]{7})", re.IGNORECASE
)


@dataclass
class VenueCandidate:
    id: str
    name: str
    type: str | None
    works_count: int | None


@dataclass
class SourceStats:
    id: str
    mean_citedness: float | None  # 2-year mean citedness, an impact factor analogue
    h_index: int | None


class OpenAlexClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        api_key: str | None,
        http: httpx.Client | None = None,
        daily_usd: float = 1.0,
        max_age_days: int = 30,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.conn = conn
        self.api_key = api_key
        self.http = http or httpx.Client(
            base_url=BASE_URL,
            timeout=30,
            headers={"User-Agent": f"AriadneSearch/{__version__}"},
        )
        self.daily_usd = daily_usd
        self.max_age = timedelta(days=max_age_days)
        self.sleep = sleep

    def get_json(
        self, path: str, params: dict | None = None, read_cache: bool = True
    ) -> dict | None:
        """GET with cache and backoff. Return None on 404.

        Responses are stored without the API key, in cache.db. A request made
        with `read_cache=False`, such as a page of citing papers, is not stored
        either, since it would never be read. Spending is added to `budget`, and
        calls stop once the daily limit is reached.
        """
        params = dict(params or {})
        url = f"{path}?{urlencode(sorted(params.items()))}"
        key = hashlib.sha256(url.encode()).hexdigest()
        if read_cache:
            cached = self._read_cache(key)
            if cached is not None:
                return cached

        spent = usage_today(self.conn, "openalex")[1]
        if spent >= self.daily_usd:
            raise BudgetExceeded(
                f"OpenAlex daily budget reached (${spent:.4f} of "
                f"${self.daily_usd:.2f}). It resets at midnight UTC."
            )

        response = self._send(path, params)
        if response.status_code == 404:
            add_usage(self.conn, "openalex", 1)
            self.conn.commit()
            return None

        body = response.json()
        cost = (body.get("meta") or {}).get("cost_usd") or 0.0
        add_usage(self.conn, "openalex", 1, float(cost))
        if read_cache:
            self._write_cache(key, url, body)
        else:
            self.conn.commit()
        return body

    def _send(self, path: str, params: dict) -> httpx.Response:
        request_params = dict(params)
        if self.api_key:
            request_params["api_key"] = self.api_key

        wait = 1.0
        for _ in range(MAX_RETRIES):
            try:
                response = self.http.get(path, params=request_params)
            except httpx.TransportError:
                self.sleep(wait)
                wait = min(wait * 2, 30)
                continue

            status = response.status_code
            if status == 429 and response.headers.get("X-RateLimit-Remaining") == "0":
                raise BudgetExceeded(
                    "OpenAlex reports that the daily budget is used up. "
                    "It resets at midnight UTC."
                )
            if status == 429 or status >= 500:
                retry_after = response.headers.get("Retry-After", "")
                self.sleep(float(retry_after) if retry_after.isdigit() else wait)
                wait = min(wait * 2, 30)
                continue
            if status in (401, 403):
                if self.api_key:
                    raise UpstreamError("OpenAlex rejected the API key. Check `.env`.")
                raise MissingKey(
                    "OpenAlex needs an API key for this request, such as paging past "
                    "the first 100 results. Get a free key at "
                    "https://openalex.org/settings/api and set OPENALEX_API_KEY in `.env`."
                )
            if status == 404 or status < 400:
                return response
            detail = response.text[:200].replace(self.api_key or "\0", "***")
            raise UpstreamError(f"OpenAlex returned HTTP {status} for {path}: {detail}")
        raise UpstreamError(f"OpenAlex is not responding for {path}. Try again later.")

    def _read_cache(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT body, fetched_at FROM cache.raw_response WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at > self.max_age:
            return None
        return json.loads(zlib.decompress(row["body"]))

    def _write_cache(self, key: str, url: str, body: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cache.raw_response(key, url, body, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (key, url, zlib.compress(json.dumps(body).encode()), utc_now()),
        )
        self.conn.commit()

    def _fetch_all(
        self, path: str, params: dict, cap: int, cached: bool = False
    ) -> tuple[list[dict], int]:
        """Follow cursor pages up to `cap` results. Return (results, total).

        Pages are fetched fresh unless `cached`, for lists that barely change.
        """
        results: list[dict] = []
        total = 0
        cursor: str | None = "*"
        while cursor and len(results) < cap:
            page_params = {
                **params,
                "per_page": min(MAX_PER_PAGE, cap - len(results)),
                "cursor": cursor,
            }
            body = self.get_json(path, page_params, read_cache=cached) or {}
            meta = body.get("meta") or {}
            total = int(meta.get("count") or total)
            page = body.get("results") or []
            results.extend(page)
            cursor = meta.get("next_cursor") if page else None
        return results[:cap], total

    def work_by_doi(self, doi: str) -> WorkRecord | None:
        """Fetch one work, free of charge. Return None if it is unknown."""
        path = f"/works/doi:{quote(normalize_doi(doi), safe='/()')}"
        body = self.get_json(path, {"select": SELECT_FIELDS})
        return parse_work(body) if body else None

    def work_by_id(self, openalex_id: str) -> WorkRecord | None:
        """Fetch one work by its OpenAlex ID."""
        path = f"/works/{short_id(openalex_id)}"
        body = self.get_json(path, {"select": SELECT_FIELDS})
        return parse_work(body) if body else None

    def works_by_ids(self, openalex_ids: list[str]) -> list[WorkRecord]:
        """Batch fetch with `filter=openalex:W1|W2`, 100 IDs per call."""
        records: list[WorkRecord] = []
        ids = sorted({short_id(item) for item in openalex_ids})
        for chunk in chunks(ids, MAX_OR_VALUES):
            body = self.get_json(
                "/works",
                {
                    "filter": "openalex:" + "|".join(chunk),
                    "per_page": MAX_PER_PAGE,
                    "select": SELECT_FIELDS,
                },
            )
            records.extend(parse_work(item) for item in (body or {}).get("results", []))
        return records

    def works_by_dois(self, dois: list[str]) -> list[WorkRecord]:
        """Batch fetch with `filter=doi:a|b`.

        DOIs containing filter syntax are fetched one at a time.
        """
        clean = sorted({normalize_doi(doi) for doi in dois})
        batchable = [doi for doi in clean if not set("|,") & set(doi)]
        records: list[WorkRecord] = []
        for chunk in chunks(batchable, MAX_DOIS_PER_CALL):
            body = self.get_json(
                "/works",
                {
                    "filter": "doi:" + "|".join(chunk),
                    "per_page": MAX_PER_PAGE,
                    "select": SELECT_FIELDS,
                },
            )
            records.extend(parse_work(item) for item in (body or {}).get("results", []))
        for doi in set(clean) - set(batchable):
            record = self.work_by_doi(doi)
            if record:
                records.append(record)
        return records

    def fetch_citers(
        self,
        openalex_ids: list[str],
        cap: int,
        since: date | None = None,
        order: CiterOrder = "newest",
    ) -> tuple[list[WorkRecord], int]:
        """Return up to `cap` citing works and the total count.

        `order` picks which come first: the newest, or the most cited. The most
        cited change slowly, so those pages are cached. With `since`, only works
        published on or after that date are returned.
        """
        filter_value = "cites:" + "|".join(short_id(item) for item in openalex_ids)
        if since:
            filter_value += f",from_publication_date:{since.isoformat()}"
        results, total = self._fetch_all(
            "/works",
            {
                "filter": filter_value,
                "sort": CITER_SORTS[order],
                "select": SELECT_FIELDS,
            },
            cap,
            cached=order == "most_cited",
        )
        return [parse_work(item) for item in results], total

    def search(self, query: str, per_page: int = 25) -> list[WorkRecord]:
        """Keyword search. This costs 10 times a list call."""
        body = self.get_json(
            "/works",
            {"search": query, "per_page": per_page, "select": SELECT_FIELDS},
        )
        return [parse_work(item) for item in (body or {}).get("results", [])]

    def search_sources(self, name: str, per_page: int = 5) -> list[VenueCandidate]:
        """Find venues by name."""
        body = self.get_json("/sources", {"search": name, "per_page": per_page})
        return [
            VenueCandidate(
                id=short_id(item["id"]),
                name=item.get("display_name") or "",
                type=item.get("type"),
                works_count=item.get("works_count"),
            )
            for item in (body or {}).get("results", [])
        ]

    def topics_by_ids(self, openalex_ids: list[str]) -> dict[str, dict]:
        """Return each work's primary topic, 100 per call. Works without one are left out."""
        topics: dict[str, dict] = {}
        for chunk in chunks(sorted({short_id(item) for item in openalex_ids}), MAX_OR_VALUES):
            body = self.get_json(
                "/works",
                {
                    "filter": "openalex:" + "|".join(chunk),
                    "per_page": MAX_PER_PAGE,
                    "select": "id,primary_topic",
                },
            )
            for item in (body or {}).get("results", []):
                if item.get("primary_topic"):
                    topics[short_id(item["id"])] = item["primary_topic"]
        return topics

    def sources_by_ids(self, source_ids: list[str]) -> list[SourceStats]:
        """Batch fetch venue impact figures with `filter=openalex:S1|S2`."""
        stats: list[SourceStats] = []
        ids = sorted({short_id(item) for item in source_ids})
        for chunk in chunks(ids, MAX_OR_VALUES):
            body = self.get_json(
                "/sources",
                {
                    "filter": "openalex:" + "|".join(chunk),
                    "per_page": MAX_PER_PAGE,
                    "select": "id,summary_stats",
                },
            )
            for item in (body or {}).get("results", []):
                summary = item.get("summary_stats") or {}
                stats.append(
                    SourceStats(
                        id=short_id(item["id"]),
                        mean_citedness=summary.get("2yr_mean_citedness"),
                        h_index=summary.get("h_index"),
                    )
                )
        return stats

    def works_by_source(
        self, source_id: str, since_year: int, cap: int = 200
    ) -> list[WorkRecord]:
        """Return a venue's most cited papers since a year."""
        results, _ = self._fetch_all(
            "/works",
            {
                "filter": (
                    f"primary_location.source.id:{short_id(source_id)},"
                    f"from_publication_date:{since_year}-01-01"
                ),
                "sort": "cited_by_count:desc",
                "select": SELECT_FIELDS,
            },
            cap,
        )
        return [parse_work(item) for item in results]


def short_id(openalex_id: str) -> str:
    """Return the `W123` form of an OpenAlex ID or URL."""
    return openalex_id.rstrip("/").rsplit("/", 1)[-1]


def decode_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """Rebuild abstract text. Some records have a null index."""
    if not inverted_index:
        return None
    words = {
        position: word
        for word, positions in inverted_index.items()
        for position in positions
    }
    return " ".join(words[position] for position in sorted(words))


def find_arxiv_id(raw: dict) -> str | None:
    """Find an arXiv ID. OpenAlex's `ids` object has no arXiv field."""
    doi = normalize_doi(raw.get("doi") or "")
    if doi.startswith("10.48550/arxiv."):
        return clean_arxiv_id(doi.removeprefix("10.48550/arxiv."))
    for location in raw.get("locations") or []:
        for key in ("landing_page_url", "pdf_url"):
            match = _ARXIV_URL.search((location or {}).get(key) or "")
            if match:
                return clean_arxiv_id(match.group(1))
    return None


def parse_work(raw: dict) -> WorkRecord:
    """Convert one API result into a WorkRecord."""
    location = raw.get("primary_location") or {}
    source = location.get("source") or {}
    percentile = raw.get("citation_normalized_percentile") or {}
    topic = raw.get("primary_topic") or {}
    references = [short_id(item) for item in raw.get("referenced_works") or []]
    authors = [
        (item.get("author") or {}).get("display_name") or item.get("raw_author_name")
        for item in raw.get("authorships") or []
    ]
    return WorkRecord(
        openalex_id=short_id(raw["id"]),
        doi=normalize_doi(raw["doi"]) if raw.get("doi") else None,
        arxiv_id=find_arxiv_id(raw),
        title=raw.get("title"),
        year=raw.get("publication_year"),
        pub_date=raw.get("publication_date"),
        type=raw.get("type"),
        venue=source.get("display_name"),
        venue_id=short_id(source["id"]) if source.get("id") else None,
        venue_type=source.get("type"),
        authors=[name for name in authors if name],
        abstract=decode_abstract(raw.get("abstract_inverted_index")),
        cited_by_count=raw.get("cited_by_count"),
        percentile=percentile.get("value"),
        referenced_ids=references,
        n_refs=int(raw.get("referenced_works_count") or len(references)),
        topic=topic.get("display_name"),
        subfield=(topic.get("subfield") or {}).get("display_name"),
        field=(topic.get("field") or {}).get("display_name"),
    )
