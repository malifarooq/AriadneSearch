"""A fake OpenAlex that follows the documented API shape and limits."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from ariadnesearch.db import create_project, init_db
from ariadnesearch.openalex import BASE_URL, OpenAlexClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

LIST_COST = 0.0001
SEARCH_COST = 0.001


def work(
    number: int,
    title: str,
    year: int = 2023,
    refs: tuple[int, ...] = (),
    cited_by: int = 10,
    doi: str | None = None,
    work_type: str = "article",
    venue: tuple[str, str, str] | None = ("S1", "Test Journal", "journal"),
    authors: tuple[str, ...] = ("Ada Lovelace",),
    percentile: float | None = 0.5,
    abstract: dict | None = None,
    arxiv: str | None = None,
    topic: tuple[str, str, str] | None = None,
    author_ids: tuple[str, ...] = (),
) -> dict:
    """Build one work in the OpenAlex response format. `topic` is (topic, subfield, field).

    `author_ids` gives OpenAlex author IDs to the first authors, in order.
    """
    source = (
        {
            "id": f"https://openalex.org/{venue[0]}",
            "display_name": venue[1],
            "type": venue[2],
        }
        if venue
        else None
    )
    location = {"is_oa": False, "landing_page_url": None, "pdf_url": None, "source": source}
    locations = [location]
    if arxiv:
        locations.append(
            {"landing_page_url": f"https://arxiv.org/abs/{arxiv}", "source": None}
        )
    return {
        "id": f"https://openalex.org/W{number}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "ids": {"openalex": f"https://openalex.org/W{number}"},
        "title": title,
        "publication_year": year,
        "publication_date": f"{year}-06-01",
        "type": work_type,
        "primary_location": location,
        "locations": locations,
        "authorships": [
            {
                "author": {
                    "display_name": name,
                    **({"id": f"https://openalex.org/{author_ids[index]}"} if index < len(author_ids) else {}),
                },
                "raw_author_name": name,
            }
            for index, name in enumerate(authors)
        ],
        "abstract_inverted_index": abstract,
        "cited_by_count": cited_by,
        "citation_normalized_percentile": (
            {"value": percentile, "is_in_top_1_percent": False, "is_in_top_10_percent": False}
            if percentile is not None
            else None
        ),
        "referenced_works": [f"https://openalex.org/W{ref}" for ref in refs],
        "referenced_works_count": len(refs),
        "primary_topic": (
            {
                "display_name": topic[0],
                "subfield": {"display_name": topic[1]},
                "field": {"display_name": topic[2]},
            }
            if topic
            else None
        ),
    }


def author(
    author_id: str,
    name: str,
    works_count: int = 10,
    cited_by: int = 100,
    orcid: str | None = None,
    alternatives: tuple[str, ...] = (),
) -> dict:
    """Build one author in the OpenAlex response format."""
    return {
        "id": f"https://openalex.org/{author_id}",
        "display_name": name,
        "display_name_alternatives": [name, *alternatives],
        "orcid": f"https://orcid.org/{orcid}" if orcid else None,
        "works_count": works_count,
        "cited_by_count": cited_by,
        "summary_stats": {"h_index": 5, "2yr_mean_citedness": 1.0},
        "last_known_institutions": [{"display_name": "Test University"}],
        "topics": [{"display_name": "Readout"}],
        "counts_by_year": [{"year": 2015, "works_count": 2}, {"year": 2020, "works_count": 1}],
    }


class FakeOpenAlex:
    """An in-memory API. It rejects requests that break documented limits."""

    def __init__(
        self,
        works: list[dict],
        sources: list[dict] | None = None,
        authors: list[dict] | None = None,
    ):
        self.works = {item["id"].rsplit("/", 1)[-1]: item for item in works}
        self.sources = sources or []
        self.authors = authors or []
        self.calls: list[httpx.URL] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url)
        path = request.url.path
        params = dict(request.url.params)
        per_page = int(params.get("per_page", 25))
        if per_page > 100:
            return httpx.Response(400, json={"error": "per_page too large"})

        if path == "/sources":
            return httpx.Response(
                200,
                json={
                    "meta": {"count": len(self.sources), "cost_usd": SEARCH_COST},
                    "results": self.sources,
                },
            )
        if path == "/works":
            return self._list(params, per_page)
        if path == "/authors":
            term = params.get("search", "").lower()
            found = [
                item for item in self.authors
                if any(word in item["display_name"].lower() for word in term.split())
            ]
            return httpx.Response(
                200,
                json={"meta": {"count": len(found), "cost_usd": SEARCH_COST}, "results": found},
            )
        if path.startswith("/authors/"):
            wanted = path.removeprefix("/authors/")
            for item in self.authors:
                orcid = (item["orcid"] or "").rsplit("/", 1)[-1]
                if wanted in (item["id"].rsplit("/", 1)[-1], f"orcid:{orcid}"):
                    return httpx.Response(200, json=item)
            return httpx.Response(404, json={})

        identifier = path.removeprefix("/works/")
        if identifier.startswith("doi:"):
            found = self._by_doi(identifier.removeprefix("doi:"))
            found = found[0] if found else None
        else:
            found = self.works.get(identifier)
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={})

    def _by_doi(self, doi: str) -> list[dict]:
        wanted = f"https://doi.org/{doi}"
        return [item for item in self.works.values() if item["doi"] == wanted]

    def _list(self, params: dict, per_page: int) -> httpx.Response:
        cost = LIST_COST
        if "search" in params:
            cost = SEARCH_COST
            term = params["search"].lower()
            matches = [
                item for item in self.works.values() if term in item["title"].lower()
            ]
        else:
            matches = list(self.works.values())
            for clause in params.get("filter", "").split(","):
                name, _, value = clause.partition(":")
                values = value.split("|")
                if len(values) > 100:
                    return httpx.Response(400, json={"error": "too many OR values"})
                matches = self._apply(matches, name, value, values)

        sort = params.get("sort")
        if sort and sort.startswith("-"):  # the live API rejects this form
            return httpx.Response(400, json={"error": "Invalid query parameters error."})
        if sort == "publication_date:desc":
            matches.sort(key=lambda item: item["publication_date"], reverse=True)
        if sort == "cited_by_count:desc":
            matches.sort(key=lambda item: item["cited_by_count"], reverse=True)

        cursor = params.get("cursor")
        start = 0 if cursor in (None, "*") else int(cursor)
        page = matches[start : start + per_page]
        end = start + per_page
        return httpx.Response(
            200,
            json={
                "meta": {
                    "count": len(matches),
                    "cost_usd": cost,
                    "next_cursor": str(end) if cursor and end < len(matches) else None,
                },
                "results": page,
            },
        )

    def _apply(
        self, matches: list[dict], name: str, value: str, values: list[str]
    ) -> list[dict]:
        if name == "openalex":
            return [item for item in matches if item["id"].rsplit("/", 1)[-1] in values]
        if name == "doi":
            wanted = {f"https://doi.org/{item}" for item in values}
            return [item for item in matches if item["doi"] in wanted]
        if name == "cites":
            targets = {f"https://openalex.org/{item}" for item in values}
            return [item for item in matches if targets & set(item["referenced_works"])]
        if name == "primary_location.source.id":
            return [
                item
                for item in matches
                if item["primary_location"]["source"]
                and item["primary_location"]["source"]["id"].endswith(value)
            ]
        if name == "author.id":
            wanted = {f"https://openalex.org/{item}" for item in values}
            return [
                item
                for item in matches
                if wanted & {(entry["author"] or {}).get("id") for entry in item["authorships"]}
            ]
        if name == "from_publication_date":
            return [item for item in matches if item["publication_date"] >= value]
        raise AssertionError(f"Unexpected filter: {name}")


def small_field() -> list[dict]:
    """A small citation network with two seeds.

    Seeds 1 and 2 share references 10 and 11. Papers 20 to 22 cite the seeds and
    also cite paper 30, so 30 is co-cited with the seeds without being linked to
    them directly. Paper 40 shares references with the seeds but does not cite
    them. Paper 50 has no connection.
    """
    return [
        work(1, "Readout discrimination on FPGAs", 2022, (10, 11, 12), 40, "10.1109/seed.1"),
        work(2, "Low latency qubit classification", 2023, (10, 11, 13), 30, "10.1145/seed.2"),
        work(10, "Matched filtering foundations", 2005, (), 900, "10.1000/ref.10", percentile=0.99),
        work(11, "Superconducting qubit measurement", 2012, (), 700, "10.1000/ref.11", percentile=0.95),
        work(12, "A reference only seed one uses", 2015, (), 50, "10.1000/ref.12"),
        work(13, "A reference only seed two uses", 2016, (), 40, "10.1000/ref.13"),
        work(20, "Neural readout at the edge", 2024, (1, 2, 30), 8, "10.1000/citer.20"),
        work(21, "FPGA inference for quantum control", 2024, (1, 2, 30), 6, "10.1000/citer.21"),
        work(22, "Real time discrimination systems", 2025, (1, 30), 2, "10.1000/citer.22"),
        work(30, "Dispersive readout theory", 2010, (), 500, "10.1000/cocited.30", percentile=0.9),
        work(40, "Unrelated title, shared foundations", 2021, (10, 11), 12, "10.1000/coupled.40"),
        work(50, "Completely separate subject", 2020, (), 3, "10.1000/other.50"),
    ]


HOPPER = ("Grace Hopper",)  # an author list for `author_career`


def author_career() -> list[dict]:
    """Grace Hopper's papers, A1, and one more under a second profile, A2.

    Paper 201 is cited by three of her papers, 205 and 206 by two from 2019 on.
    Paper 300 lists her as an author but is missing from her profile.
    """
    return [
        work(101, "Compilers for everyone", 2015, (201, 202, 203),
             authors=("Grace Hopper", "Alan Turing"), author_ids=("A1",),
             topic=("Compilers", "Software", "Computer Science")),
        work(102, "Compilers that read English", 2016, (101, 201, 204),
             authors=("Grace Hopper", "Alan Turing"), author_ids=("A1",),
             topic=("Compilers", "Software", "Computer Science")),
        work(103, "Debugging moths at scale", 2019, (201, 205, 206, 300),
             authors=("Grace Hopper", "Barbara Liskov"), author_ids=("A1",),
             topic=("Debugging", "Software", "Computer Science")),
        work(104, "Nanoseconds made visible", 2020, (205, 206, 102), cited_by=90,
             authors=("Grace Hopper",), author_ids=("A1",),
             topic=("Debugging", "Software", "Computer Science")),
        work(105, "Front matter", 2020, work_type="paratext",
             authors=HOPPER, author_ids=("A1",)),
        work(106, "Standards for business languages", 2021, (201,),
             authors=("G. Hopper",), author_ids=("A2",)),
        work(201, "Notes on the analytical engine", 1843, cited_by=500,
             authors=("Ada Lovelace",)),
        work(202, "On computable numbers", 1936, authors=("Alan Turing",)),
        work(203, "A first draft report", 1945, authors=("John von Neumann",)),
        work(204, "The mathematical theory of communication", 1948, authors=("Claude Shannon",)),
        work(205, "Abstraction in programming", 2010, authors=("Barbara Liskov",)),
        work(206, "Structured programming notes", 2012, authors=("Edsger Dijkstra",)),
        work(300, "The first compiler", 2014, authors=("Grace Hopper",)),
        work(400, "A later paper citing her", 2022, (104,), authors=("Someone Else",)),
    ]


def author_profiles() -> list[dict]:
    """Profiles for `author_career`, and two that only match a name search."""
    return [
        author("A1", "Grace Hopper", works_count=5, orcid="0000-0002-1825-0097"),
        author("A2", "G. Hopper", works_count=1),
        author("A9", "Grace Hopper Anderson Smith Jones", works_count=1),
        author("A8", "Hopper", works_count=400),
    ]


@pytest.fixture(autouse=True)
def no_arxiv(monkeypatch):
    """Tests never reach arXiv. A test that needs a source bundle patches this."""
    monkeypatch.setattr("ariadnesearch.references.fetch_arxiv_source", lambda arxiv_id: None)


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "graph.db")
    yield connection
    connection.close()


@pytest.fixture
def fake():
    return FakeOpenAlex(small_field())


def make_client(connection, handler, api_key="test-key", **kwargs) -> OpenAlexClient:
    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return OpenAlexClient(connection, api_key, http=http, sleep=lambda _: None, **kwargs)


@pytest.fixture
def client(conn, fake):
    return make_client(conn, fake)


@pytest.fixture
def project(conn):
    return create_project(conn, "readout")
