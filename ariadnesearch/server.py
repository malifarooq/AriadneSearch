"""Local web server. `ariadne serve` binds it to 127.0.0.1 only."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ariadnesearch import views
from ariadnesearch.config import REPO_ROOT, load_config
from ariadnesearch.db import connect, create_project as insert_project, init_db
from ariadnesearch.errors import (
    BadInput,
    BudgetExceeded,
    AriadneError,
    NotFound,
    UpstreamError,
)
from ariadnesearch.expand import add_seeds as add_project_seeds
from ariadnesearch.expand import (
    PER_PAPER_LIMIT,
    expand_project,
    pending_count,
    record_decision,
    update_project,
)
from ariadnesearch.ieee import IEEEClient, is_ieee_doi, open_store
from ariadnesearch.openalex import OpenAlexClient
from ariadnesearch.rank import load_weights
from ariadnesearch.tune import GRID, MIN_SEEDS, tune_project
from ariadnesearch.venues import (
    add_target,
    add_venue_papers,
    find_venues,
    list_targets,
    remove_target,
    unmatched_venues,
)

WEB_DIR = REPO_ROOT / "web"
RankFilter = Literal["A*", "A", "B", "C"]


class ProjectRequest(BaseModel):
    name: str


class SeedRequest(BaseModel):
    identifiers: list[str] = Field(min_length=1)


class ExpandRequest(BaseModel):
    top_k: int = Field(default=20, ge=0, le=200)
    per_paper: int | None = Field(default=PER_PAPER_LIMIT, ge=1, le=100)  # None: all


class DecisionRequest(BaseModel):
    work_id: int
    decision: Literal["accepted", "rejected", "read"]


class Position(BaseModel):
    id: int
    x: float
    y: float


class PositionsRequest(BaseModel):
    positions: list[Position]


class IEEESearchRequest(BaseModel):
    query: str = Field(min_length=1)
    max_records: int = Field(default=25, ge=1, le=200)


class VenueSearchRequest(BaseModel):
    name: str = Field(min_length=1)


class VenueAddRequest(BaseModel):
    venue_id: str
    since_year: int = Field(ge=1900, le=2100)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db(load_config().graph_db).close()
    yield


app = FastAPI(title="AriadneSearch", lifespan=lifespan)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message})


@app.exception_handler(AriadneError)
async def expected_failure(_: Request, exc: AriadneError) -> JSONResponse:
    statuses = {BudgetExceeded: 429, NotFound: 404, UpstreamError: 502}
    return _error(statuses.get(type(exc), 400), str(exc))


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(load_config().graph_db)
    try:
        yield conn
    finally:
        conn.close()


def get_client(conn: sqlite3.Connection = Depends(get_conn)) -> Iterator[OpenAlexClient]:
    config = load_config()
    client = OpenAlexClient(
        conn, config.openalex_api_key, daily_usd=config.openalex_daily_usd
    )
    try:
        yield client
    finally:
        client.http.close()


def get_ieee(conn: sqlite3.Connection = Depends(get_conn)) -> Iterator[IEEEClient]:
    config = load_config()
    store = open_store(config.ieee_db)
    client = IEEEClient(
        config.ieee_api_key,
        store,
        conn,
        daily_soft_limit=config.ieee_daily_soft_limit,
        rate_per_second=config.ieee_rate_per_second,
    )
    try:
        yield client
    finally:
        client.http.close()
        store.close()


def _require_project(conn: sqlite3.Connection, project_id: int) -> None:
    row = conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise NotFound(f"No project with ID {project_id}.")


def _doi(conn: sqlite3.Connection, work_id: int) -> str | None:
    return views.work_summaries(conn, [work_id]).get(work_id, {}).get("doi")


@app.get("/api/health")
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    config = load_config()
    return {
        "status": "ok",
        "openalex_key": bool(config.openalex_api_key),
        "ieee_key": bool(config.ieee_api_key),
        "usage": views.usage_view(conn),
        "limits": {
            "openalex_daily_usd": config.openalex_daily_usd,
            "ieee_daily_calls": config.ieee_daily_soft_limit,
        },
        "core_rows": conn.execute("SELECT COUNT(*) FROM venue_rank").fetchone()[0],
    }


@app.get("/api/projects")
def list_projects(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute("SELECT id, name FROM project ORDER BY name").fetchall()
    return {
        "projects": [
            {
                **dict(row),
                "counts": views.project_summary(conn, row["id"]),
                "pending": pending_count(conn, row["id"]),
            }
            for row in rows
        ]
    }


@app.post("/api/projects")
def create_project(
    body: ProjectRequest, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    return {"id": insert_project(conn, body.name), "name": body.name.strip()}


@app.post("/api/projects/{project_id}/seeds")
def add_seeds(
    project_id: int,
    body: SeedRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    client: OpenAlexClient = Depends(get_client),
) -> dict:
    _require_project(conn, project_id)
    return asdict(add_project_seeds(conn, client, project_id, body.identifiers))


@app.post("/api/projects/{project_id}/expand")
def expand(
    project_id: int,
    body: ExpandRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    client: OpenAlexClient = Depends(get_client),
) -> dict:
    _require_project(conn, project_id)
    config = load_config()
    report = expand_project(
        conn,
        client,
        project_id,
        body.top_k,
        citer_cap=config.citer_cap,
        per_paper=body.per_paper,
    )
    return asdict(report)


@app.post("/api/projects/{project_id}/update")
def update(
    project_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    client: OpenAlexClient = Depends(get_client),
) -> dict:
    """Add papers citing the project's known papers, published since the last run."""
    _require_project(conn, project_id)
    return asdict(update_project(conn, client, project_id))


@app.post("/api/projects/{project_id}/decisions")
def decide(
    project_id: int,
    body: DecisionRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    record_decision(conn, project_id, body.work_id, body.decision)
    return {"work_id": body.work_id, "state": body.decision}


@app.get("/api/projects/{project_id}/frontier")
def frontier(
    project_id: int,
    k: int = 50,
    hide_cited: bool = False,
    hide_library: bool = False,
    min_rank: RankFilter | None = None,
    min_year: int | None = None,
    min_impact: float | None = Query(default=None, ge=0),
    sort: views.SortOrder = "relevance",
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Return ranked papers with their reasons, flags and venue ranks."""
    _require_project(conn, project_id)
    return views.frontier_view(
        conn, project_id, k, hide_cited, hide_library, min_rank, min_year, min_impact, sort
    )


@app.get("/api/projects/{project_id}/graph")
def graph(
    project_id: int,
    limit: int = 200,
    mode: views.GraphMode = "all",
    min_year: int | None = None,
    added_only: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Return graph nodes and edges, with saved positions."""
    _require_project(conn, project_id)
    return views.graph_view(conn, project_id, limit, "added" if added_only else mode, min_year)


@app.get("/api/projects/{project_id}/search")
def search(
    project_id: int,
    q: str = "",
    limit: int = Query(default=20, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Find papers in this project by title, author, year or DOI."""
    _require_project(conn, project_id)
    return views.search_view(conn, project_id, q, limit)


class TargetRequest(BaseModel):
    title: str
    acronym: str | None = None
    rank: RankFilter


@app.get("/api/venue-targets")
def venue_targets(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Return your target venue list, which overrides CORE ranks."""
    return {"targets": list_targets(conn)}


@app.post("/api/venue-targets")
def add_venue_target(body: TargetRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    add_target(conn, body.title, body.acronym, body.rank)
    return {"targets": list_targets(conn)}


@app.delete("/api/venue-targets")
def remove_venue_target(title: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    remove_target(conn, title)
    return {"targets": list_targets(conn)}


@app.get("/api/projects/{project_id}/weights")
def weights(project_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Return the project's ranking weights and whether they were fitted."""
    _require_project(conn, project_id)
    tuned = conn.execute(
        "SELECT ts, payload_json FROM event WHERE project_id = ? AND type = 'tune' "
        "ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    return {
        "weights": {
            key: value
            for key, value in asdict(load_weights(conn, project_id)).items()
            if key in GRID
        },
        "tuned": {"at": tuned["ts"], **json.loads(tuned["payload_json"])} if tuned else None,
        "min_seeds": MIN_SEEDS,
        "papers": conn.execute(
            "SELECT COUNT(*) FROM membership WHERE project_id = ? "
            "AND state IN ('seed', 'accepted', 'read')",
            (project_id,),
        ).fetchone()[0],
    }


@app.post("/api/projects/{project_id}/tune")
def tune(
    project_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    client: OpenAlexClient = Depends(get_client),
) -> dict:
    """Fit the project's weights to its seed papers. This runs three full expansions."""
    _require_project(conn, project_id)
    best, result = tune_project(conn, client, project_id)
    _, recall = result.best()
    return {
        "weights": best,
        "recall": recall,
        "default_recall": sum(result.default_recall) / len(result.default_recall),
    }


@app.get("/api/projects/{project_id}/authors")
def authors(
    project_id: int,
    limit: int = Query(default=40, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Return the project's most frequent authors and their papers."""
    _require_project(conn, project_id)
    return views.authors_view(conn, project_id, limit)


@app.get("/api/projects/{project_id}/added")
def added(project_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Return the papers you added: seeds, accepted and read papers."""
    _require_project(conn, project_id)
    return views.added_view(conn, project_id)


@app.post("/api/projects/{project_id}/positions")
def positions(
    project_id: int,
    body: PositionsRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    views.save_positions(conn, project_id, [item.model_dump() for item in body.positions])
    return {"saved": len(body.positions)}


@app.get("/api/projects/{project_id}/export.bib", response_class=PlainTextResponse)
def export_bib(
    project_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> PlainTextResponse:
    """Export kept papers. This never includes IEEE fields."""
    _require_project(conn, project_id)
    return PlainTextResponse(
        views.export_bib(conn, project_id),
        headers={"Content-Disposition": 'attachment; filename="ariadnesearch.bib"'},
    )


@app.get("/api/projects/{project_id}/works/{work_id}")
def work(
    project_id: int,
    work_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    ieee: IEEEClient = Depends(get_ieee),
) -> dict:
    """Return paper details. This shows stored IEEE data but never calls IEEE."""
    details = views.work_view(conn, project_id, work_id)
    doi = details.get("doi")
    stored = ieee.cached(doi) if doi else None
    return {
        **details,
        "ieee": asdict(stored) if stored else None,
        "ieee_available": bool(doi and is_ieee_doi(doi)),
    }


@app.post("/api/works/{work_id}/ieee")
def ieee_details(
    work_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    ieee: IEEEClient = Depends(get_ieee),
) -> dict:
    """Fetch IEEE details when the user asks for them."""
    doi = _doi(conn, work_id)
    if not doi or not is_ieee_doi(doi):
        raise BadInput("This paper has no IEEE-hosted DOI.")
    records = ieee.lookup_dois([doi])
    return {
        "ieee": asdict(records[0]) if records else None,
        "calls_today": ieee.calls_today(),
    }


@app.post("/api/ieee/search")
def ieee_search(
    body: IEEESearchRequest, ieee: IEEEClient = Depends(get_ieee)
) -> dict:
    """Run a user-initiated IEEE search."""
    records = ieee.search(body.query, body.max_records)
    return {
        "results": [asdict(record) for record in records],
        "calls_today": ieee.calls_today(),
    }


@app.post("/api/venues/search")
def venue_search(
    body: VenueSearchRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    client: OpenAlexClient = Depends(get_client),
) -> dict:
    return {"venues": [asdict(item) for item in find_venues(conn, client, body.name)]}


@app.post("/api/projects/{project_id}/venues")
def venue_add(
    project_id: int,
    body: VenueAddRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    client: OpenAlexClient = Depends(get_client),
) -> dict:
    _require_project(conn, project_id)
    added = add_venue_papers(conn, client, project_id, body.venue_id, body.since_year)
    return {"added": added}


@app.get("/api/projects/{project_id}/venues/unmatched")
def venues_unmatched(
    project_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    _require_project(conn, project_id)
    return {"venues": unmatched_venues(conn, project_id)}


if Path(WEB_DIR).is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
