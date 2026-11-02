import pytest
from fastapi.testclient import TestClient

from conftest import FakeOpenAlex, make_client, small_field
from ariadnesearch import server
from ariadnesearch.config import load_config
from ariadnesearch.db import connect, usage_today
from ariadnesearch.ieee import BASE_URL, IEEEClient, open_store
from ariadnesearch.mine import MyPaperEntry, sync_published
from ariadnesearch.venues import import_core_csv

import httpx


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIADNE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-openalex")
    monkeypatch.setenv("IEEE_API_KEY", "secret-ieee")
    fake = FakeOpenAlex(small_field())
    ieee_calls = []

    def ieee_handler(request):
        ieee_calls.append(request.url)
        if "/articles/doi/" in request.url.path:
            doi = request.url.path.split("/articles/doi/", 1)[1]
            return httpx.Response(
                200,
                json={"articles": [{"doi": doi, "citing_paper_count": 99,
                                    "article_number": 5, "abstract": "IEEE only text"}]},
            )
        return httpx.Response(
            200, json={"articles": [{"doi": "10.1109/found.1", "title": "Found"}]}
        )

    def override_client(conn=server.Depends(server.get_conn)):
        yield make_client(conn, fake)

    def override_ieee(conn=server.Depends(server.get_conn)):
        store = open_store(load_config().ieee_db)
        http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(ieee_handler))
        yield IEEEClient("secret-ieee", store, conn, http=http, sleep=lambda _: None)
        store.close()

    server.app.dependency_overrides[server.get_client] = override_client
    server.app.dependency_overrides[server.get_ieee] = override_ieee
    with TestClient(server.app) as client:
        client.fake = fake
        client.ieee_calls = ieee_calls
        yield client
    server.app.dependency_overrides.clear()


def project_with_graph(api) -> int:
    project_id = api.post("/api/projects", json={"name": "readout"}).json()["id"]
    response = api.post(
        f"/api/projects/{project_id}/seeds",
        json={"identifiers": ["10.1109/seed.1", "10.1145/seed.2"]},
    )
    assert len(response.json()["added"]) == 2
    assert api.post(f"/api/projects/{project_id}/expand", json={}).status_code == 200
    return project_id


def test_update_needs_an_expand_then_reports_new_papers(api):
    project_id = api.post("/api/projects", json={"name": "fresh"}).json()["id"]
    assert api.post(f"/api/projects/{project_id}/update").status_code == 400
    project_id = project_with_graph(api)
    response = api.post(f"/api/projects/{project_id}/update")
    assert response.status_code == 200
    assert {"since", "new_papers", "cost_usd", "capped"} <= set(response.json())


def test_added_papers_mode_and_pending_count(api):
    project_id = api.post("/api/projects", json={"name": "fresh"}).json()["id"]
    api.post(f"/api/projects/{project_id}/seeds", json={"identifiers": ["10.1109/seed.1"]})
    project = next(p for p in api.get("/api/projects").json()["projects"] if p["id"] == project_id)
    assert project["pending"] == 1
    added = api.get(f"/api/projects/{project_id}/added").json()["items"]
    assert [item["state"] for item in added] == ["seed"]
    assert added[0]["populated"] is False

    assert api.post(f"/api/projects/{project_id}/expand", json={}).status_code == 200
    project = next(p for p in api.get("/api/projects").json()["projects"] if p["id"] == project_id)
    assert project["pending"] == 0
    assert api.get(f"/api/projects/{project_id}/added").json()["items"][0]["populated"]

    full = api.get(f"/api/projects/{project_id}/graph").json()
    only = api.get(f"/api/projects/{project_id}/graph?added_only=true").json()
    assert len(full["nodes"]) > 1
    assert [node["state"] for node in only["nodes"]] == ["seed"]


def test_graph_modes_and_importance_fields(api):
    project_id = project_with_graph(api)
    nodes = api.get(f"/api/projects/{project_id}/graph").json()["nodes"]
    assert {"percentile", "score", "project_citations", "pub_date"} <= set(nodes[0])
    assert any(node["score"] is not None for node in nodes if node["state"] == "frontier")

    recent = api.get(f"/api/projects/{project_id}/graph?mode=recent&min_year=2024").json()
    assert recent["nodes"] and all(node["year"] >= 2024 for node in recent["nodes"])
    added = api.get(f"/api/projects/{project_id}/graph?mode=added").json()
    assert {node["state"] for node in added["nodes"]} <= {"seed", "accepted", "read"}

    items = api.get(f"/api/projects/{project_id}/frontier?min_year=2024").json()["items"]
    assert items and all(item["year"] >= 2024 for item in items)


def test_search_finds_papers_by_title_author_year_and_doi(api):
    project_id = project_with_graph(api)

    def found(query):
        response = api.get(f"/api/projects/{project_id}/search", params={"q": query})
        assert response.status_code == 200
        return [item["title"] for item in response.json()["items"]]

    assert found("readout discrimination")[0] == "Readout discrimination on FPGAs"
    assert "Readout discrimination on FPGAs" in found("lovelace 2022")  # author and year
    assert found("10.1109/seed") == ["Readout discrimination on FPGAs"]
    assert found("dispersive")[0] == "Dispersive readout theory"
    assert found("no such words here") == []
    assert found("%") == []  # wildcards are matched literally
    assert found("") == []
    items = api.get(f"/api/projects/{project_id}/search", params={"q": "readout"}).json()["items"]
    assert items[0]["state"] == "seed"  # your own papers come first
    assert {"id", "label", "year", "authors"} <= set(items[0])


def test_target_venues_api(api):
    body = {"title": "Physical Review Applied", "acronym": None, "rank": "A"}
    assert api.post("/api/venue-targets", json=body).json()["targets"][0]["rank"] == "A"
    assert len(api.get("/api/venue-targets").json()["targets"]) == 1
    assert api.post("/api/venue-targets", json={**body, "rank": "best"}).status_code == 422
    response = api.delete("/api/venue-targets", params={"title": "Physical Review Applied"})
    assert response.json()["targets"] == []
    assert api.delete("/api/venue-targets", params={"title": "Nope"}).status_code == 404


def test_suggestions_sort_and_filter_by_journal_impact(api):
    project_id = project_with_graph(api)
    newest = api.get(f"/api/projects/{project_id}/frontier", params={"sort": "newest"}).json()["items"]
    years = [item["year"] for item in newest]
    assert years == sorted(years, reverse=True)
    cited = api.get(f"/api/projects/{project_id}/frontier", params={"sort": "citations"}).json()["items"]
    assert [item["cited_by"] for item in cited] == sorted((item["cited_by"] for item in cited), reverse=True)
    assert api.get(f"/api/projects/{project_id}/frontier", params={"sort": "hottest"}).status_code == 422
    # The test journal has no impact figures, so a minimum leaves nothing.
    none = api.get(f"/api/projects/{project_id}/frontier", params={"min_impact": 1}).json()
    assert none["items"] == []


def test_health_reports_key_presence_without_values(api):
    response = api.get("/api/health")
    body = response.json()
    assert body["status"] == "ok"
    assert body["openalex_key"] is True
    assert "secret" not in response.text


def test_project_names_must_be_unique(api):
    assert api.post("/api/projects", json={"name": "a"}).status_code == 200
    response = api.post("/api/projects", json={"name": "a"})
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_project_list_includes_counts(api):
    project_id = project_with_graph(api)
    projects = api.get("/api/projects").json()["projects"]
    assert projects[0]["id"] == project_id
    assert projects[0]["counts"]["seed"] == 2


def test_unknown_project_is_a_404(api):
    assert api.get("/api/projects/999/frontier").status_code == 404
    assert api.post("/api/projects/999/expand", json={}).status_code == 404


def test_unresolved_seeds_are_reported(api):
    project_id = api.post("/api/projects", json={"name": "p"}).json()["id"]
    response = api.post(
        f"/api/projects/{project_id}/seeds", json={"identifiers": ["10.9999/none"]}
    )
    assert response.json()["unresolved"] == ["10.9999/none"]


def test_frontier_explains_each_suggestion(api):
    project_id = project_with_graph(api)
    body = api.get(f"/api/projects/{project_id}/frontier?k=5").json()
    assert body["total"] >= 7
    assert len(body["items"]) == 5
    first = body["items"][0]
    assert first["title"]
    assert first["reasons"]
    assert "bc" in first["terms"] or "cc" in first["terms"]
    assert first["mine"]["cited_by_me"] is False
    scores = [item["score"] for item in body["items"] if not item["explore"]]
    assert scores == sorted(scores, reverse=True)


def test_previously_cited_filter(api):
    project_id = project_with_graph(api)
    conn = connect(load_config().graph_db)
    sync_published(
        conn,
        make_client(conn, api.fake),
        [MyPaperEntry("Mine", "10.1109/seed.1", "published")],
    )
    conn.close()

    everything = api.get(f"/api/projects/{project_id}/frontier").json()["items"]
    flagged = [item for item in everything if item["mine"]["cited_by_me"]]
    assert flagged
    assert flagged[0]["mine"]["cited_in"] == ["Mine"]

    hidden = api.get(f"/api/projects/{project_id}/frontier?hide_cited=true").json()["items"]
    assert not any(item["mine"]["cited_by_me"] for item in hidden)


def test_rank_filter_keeps_only_ranked_venues(api, tmp_path):
    project_id = project_with_graph(api)
    conn = connect(load_config().graph_db)
    path = tmp_path / "core.csv"
    path.write_text("Title,Acronym,Source,Rank\nTest Journal,TJ,ICORE2026,A\n")
    import_core_csv(conn, path)
    conn.close()

    items = api.get(f"/api/projects/{project_id}/frontier?min_rank=A").json()["items"]
    assert items
    assert all(item["rank"]["rank"] == "A" for item in items)
    assert api.get(f"/api/projects/{project_id}/frontier?min_rank=Z").status_code == 422


def test_decisions_change_the_frontier(api):
    project_id = project_with_graph(api)
    first = api.get(f"/api/projects/{project_id}/frontier").json()["items"][0]
    response = api.post(
        f"/api/projects/{project_id}/decisions",
        json={"work_id": first["id"], "decision": "rejected"},
    )
    assert response.status_code == 200
    ids = [item["id"] for item in api.get(f"/api/projects/{project_id}/frontier").json()["items"]]
    assert first["id"] not in ids

    bad = api.post(
        f"/api/projects/{project_id}/decisions",
        json={"work_id": "missing", "decision": "rejected"},
    )
    assert bad.status_code == 404


def test_graph_has_nodes_and_edges_within_the_limit(api):
    project_id = project_with_graph(api)
    body = api.get(f"/api/projects/{project_id}/graph?limit=6").json()
    assert len(body["nodes"]) == 6
    ids = {node["id"] for node in body["nodes"]}
    assert {edge["source"] for edge in body["edges"]} <= ids
    assert {edge["target"] for edge in body["edges"]} <= ids
    assert sum(node["state"] == "seed" for node in body["nodes"]) == 2
    assert body["edges"]


def test_positions_persist(api):
    project_id = project_with_graph(api)
    node = api.get(f"/api/projects/{project_id}/graph").json()["nodes"][0]
    api.post(
        f"/api/projects/{project_id}/positions",
        json={"positions": [{"id": node["id"], "x": 12.5, "y": -3}]},
    )
    nodes = api.get(f"/api/projects/{project_id}/graph").json()["nodes"]
    saved = next(item for item in nodes if item["id"] == node["id"])
    assert (saved["x"], saved["y"]) == (12.5, -3)


def seed_id(api, project_id, doi="10.1109/seed.1"):
    nodes = api.get(f"/api/projects/{project_id}/graph").json()["nodes"]
    for node in nodes:
        details = api.get(f"/api/projects/{project_id}/works/{node['id']}").json()
        if details.get("doi") == doi:
            return node["id"], details
    raise AssertionError(doi)


def test_opening_a_paper_never_calls_ieee(api):
    project_id = project_with_graph(api)
    _, details = seed_id(api, project_id)
    assert details["ieee_available"] is True
    assert details["ieee"] is None
    assert api.ieee_calls == []


def test_ieee_details_are_fetched_on_request_then_reused(api):
    project_id = project_with_graph(api)
    work_id, _ = seed_id(api, project_id)
    response = api.post(f"/api/works/{work_id}/ieee")
    assert response.json()["ieee"]["citing_paper_count"] == 99
    assert response.json()["calls_today"] == 1

    details = api.get(f"/api/projects/{project_id}/works/{work_id}").json()
    assert details["ieee"]["citing_paper_count"] == 99
    assert len(api.ieee_calls) == 1


def test_ieee_button_is_refused_for_other_publishers(api):
    project_id = project_with_graph(api)
    work_id, details = seed_id(api, project_id, "10.1145/seed.2")
    assert details["ieee_available"] is False
    assert api.post(f"/api/works/{work_id}/ieee").status_code == 400
    assert api.ieee_calls == []


def test_ieee_search(api):
    body = api.post("/api/ieee/search", json={"query": "qubit"}).json()
    assert body["results"][0]["doi"] == "10.1109/found.1"
    assert body["calls_today"] == 1


def test_export_is_bibtex_without_ieee_fields(api):
    project_id = project_with_graph(api)
    work_id, _ = seed_id(api, project_id)
    api.post(f"/api/works/{work_id}/ieee")
    response = api.get(f"/api/projects/{project_id}/export.bib")
    assert response.status_code == 200
    assert "@article{" in response.text
    assert "doi = {10.1109/seed.1}" in response.text
    assert "99" not in response.text
    assert "IEEE only text" not in response.text


def test_budget_errors_are_a_429(api, monkeypatch):
    monkeypatch.setenv("OPENALEX_DAILY_USD", "0")
    project_id = api.post("/api/projects", json={"name": "p"}).json()["id"]

    def spent_client(conn=server.Depends(server.get_conn)):
        yield make_client(conn, api.fake, daily_usd=0)

    server.app.dependency_overrides[server.get_client] = spent_client
    response = api.post(
        f"/api/projects/{project_id}/seeds", json={"identifiers": ["10.1109/seed.1"]}
    )
    assert response.status_code == 429
    assert "budget" in response.json()["detail"]


def test_web_page_is_served(api):
    response = api.get("/")
    assert response.status_code == 200
    assert "AriadneSearch" in response.text


def test_usage_is_counted_per_day(api):
    project_with_graph(api)
    conn = connect(load_config().graph_db)
    assert usage_today(conn, "openalex")[0] > 0
    conn.close()
