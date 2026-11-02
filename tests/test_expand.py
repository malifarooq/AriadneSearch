from datetime import date

import pytest

from conftest import FakeOpenAlex, make_client, small_field, work
from ariadnesearch.db import usage_today
from ariadnesearch.errors import BadInput, NotFound
from ariadnesearch.expand import (
    add_seeds,
    add_seeds_from_bib,
    expand_citers,
    expand_project,
    hydrate,
    record_decision,
    pending_count,
    resolve_identifier,
    update_project,
)
from ariadnesearch.identity import resolve


def members(conn, project_id, state=None):
    query = "SELECT a.value FROM membership m JOIN work_alias a ON a.work_id = m.work_id "
    query += "WHERE m.project_id = ? AND a.scheme = 'openalex'"
    args = [project_id]
    if state:
        query += " AND m.state = ?"
        args.append(state)
    return {row["value"] for row in conn.execute(query, args)}


def seed_both(conn, client, project):
    return add_seeds(conn, client, project, ["10.1109/seed.1", "10.1145/seed.2"])


@pytest.mark.parametrize(
    "text, expected",
    [
        ("10.1109/seed.1", "W1"),
        ("https://doi.org/10.1109/SEED.1", "W1"),
        ("doi:10.1109/seed.1", "W1"),
        ("W2", "W2"),
        ("https://openalex.org/W2", "W2"),
        ("Dispersive readout theory", "W30"),
    ],
)
def test_resolve_identifier(client, text, expected):
    assert resolve_identifier(client, text).openalex_id == expected


def test_arxiv_identifiers_use_the_arxiv_doi(conn):
    fake = FakeOpenAlex([work(1, "Preprint", doi="10.48550/arxiv.2401.00001")])
    client = make_client(conn, fake)
    for text in ("2401.00001", "arXiv:2401.00001v2", "https://arxiv.org/abs/2401.00001"):
        assert resolve_identifier(client, text).openalex_id == "W1"


def test_title_search_rejects_a_loose_match(client):
    assert resolve_identifier(client, "readout") is None


def test_add_seeds_reports_unresolved(conn, client, project):
    report = add_seeds(conn, client, project, ["10.1109/seed.1", "10.9999/none"])
    assert len(report.added) == 1
    assert report.unresolved == ["10.9999/none"]
    assert members(conn, project, "seed") == {"W1"}


def test_seeds_without_references_are_reported(conn, project):
    fake = FakeOpenAlex(
        [work(1, "Has refs", doi="10.1109/a", refs=(2,)), work(3, "No refs", doi="10.1109/b")]
    )
    report = add_seeds(conn, make_client(conn, fake), project, ["10.1109/a", "10.1109/b"])
    assert report.no_references == ["No refs"]


def test_seeds_from_bib(conn, client, project, tmp_path):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{a, title={One}, doi={10.1109/seed.1}}\n"
        "@article{b, title={Lost paper with no match}, year={2020}}\n"
    )
    report = add_seeds_from_bib(conn, client, project, bib)
    assert members(conn, project, "seed") == {"W1"}
    assert report.unresolved == ["b"]


def test_references_are_stored_as_stubs_at_no_cost(conn, client, project):
    seed_both(conn, client, project)
    level = conn.execute(
        "SELECT w.level FROM work w JOIN work_alias a ON a.work_id = w.id "
        "WHERE a.scheme = 'openalex' AND a.value = 'W10'"
    ).fetchone()["level"]
    assert level == "stub"


def test_hydrate_fills_stubs(conn, client, project):
    seed_both(conn, client, project)
    work_id = resolve(conn, "openalex", "W10")
    assert hydrate(conn, client, [work_id]) == 1
    row = conn.execute("SELECT * FROM work WHERE id = ?", (work_id,)).fetchone()
    assert row["title"] == "Matched filtering foundations"
    assert row["level"] == "hydrated"


def test_expand_citers_stores_edges_and_records_totals(conn, client, project):
    seed_both(conn, client, project)
    work_id = resolve(conn, "openalex", "W1")
    added, capped = expand_citers(conn, client, work_id, cap=2)
    assert added == 2
    assert capped is True
    state = conn.execute(
        "SELECT citers_total FROM fetch_state WHERE work_id = ?", (work_id,)
    ).fetchone()
    assert state["citers_total"] == 3


def test_citers_are_not_refetched_while_fresh(conn, client, fake, project):
    seed_both(conn, client, project)
    work_id = resolve(conn, "openalex", "W1")
    expand_citers(conn, client, work_id)
    calls = len(fake.calls)
    assert expand_citers(conn, client, work_id)[0] == 0
    assert len(fake.calls) == calls


def test_full_expand_adds_direct_and_indirect_papers(conn, client, project):
    seed_both(conn, client, project)
    report = expand_project(conn, client, project, per_paper=None)
    frontier = members(conn, project, "frontier")

    assert {"W10", "W11", "W12", "W13"} <= frontier  # references
    assert {"W20", "W21", "W22"} <= frontier  # citers
    assert "W30" in frontier  # co-cited but never linked directly
    assert "W40" in frontier  # coupled through expanded references
    assert "W50" not in frontier
    assert report.frontier_added >= 9
    assert report.cost_usd > 0
    assert report.cost_usd == pytest.approx(usage_today(conn, "openalex")[1])


def test_expand_hydrates_every_frontier_paper(conn, client, project):
    seed_both(conn, client, project)
    expand_project(conn, client, project)
    stubs = conn.execute(
        "SELECT COUNT(*) AS n FROM membership m JOIN work w ON w.id = m.work_id "
        "WHERE m.project_id = ? AND w.level = 'stub'",
        (project,),
    ).fetchone()["n"]
    assert stubs == 0


def test_only_top_k_frontier_papers_are_expanded(conn, client, project):
    seed_both(conn, client, project)
    report = expand_project(conn, client, project, top_k=1)
    assert report.papers_expanded <= 3  # two seeds and one frontier paper


def test_cap_is_reported(conn, client, project):
    seed_both(conn, client, project)
    report = expand_project(conn, client, project, top_k=0, citer_cap=1)
    assert len(report.cap_hit) == 2


def test_rejected_papers_stay_rejected_after_expansion(conn, client, project):
    seed_both(conn, client, project)
    expand_project(conn, client, project)
    work_id = resolve(conn, "openalex", "W20")
    record_decision(conn, project, work_id, "rejected")
    expand_project(conn, client, project)
    assert "W20" in members(conn, project, "rejected")


def test_accepted_papers_are_expanded_next_round(conn, client, project):
    seed_both(conn, client, project)
    expand_project(conn, client, project, top_k=0)
    work_id = resolve(conn, "openalex", "W30")
    record_decision(conn, project, work_id, "accepted")
    expand_project(conn, client, project, top_k=0)
    state = conn.execute(
        "SELECT citers_at FROM fetch_state WHERE work_id = ?", (work_id,)
    ).fetchone()
    assert state["citers_at"]


def test_decision_on_unknown_paper_fails(conn, project):
    with pytest.raises(NotFound):
        record_decision(conn, project, "w-missing", "accepted")


def test_decisions_are_logged(conn, client, project):
    seed_both(conn, client, project)
    expand_project(conn, client, project)
    record_decision(conn, project, resolve(conn, "openalex", "W20"), "read")
    types = [row["type"] for row in conn.execute("SELECT type FROM event ORDER BY id")]
    assert types == ["seeds", "expand", "decision"]


def test_preprint_merges_with_published_version_during_expansion(conn, project):
    works = small_field() + [
        work(60, "Readout discrimination on FPGAs", 2021, (10,), 4,
             "10.48550/arxiv.2101.00001", "preprint",
             ("S8", "arXiv", "repository")),
        work(61, "Paper citing the preprint", 2024, (60,), 1, "10.1000/x.61"),
    ]
    client = make_client(conn, FakeOpenAlex(works))
    add_seeds(conn, client, project, ["10.1109/seed.1"])
    published = resolve(conn, "openalex", "W1")
    expand_project(conn, client, project, top_k=0)

    client.work_by_id("W60")
    from ariadnesearch.identity import upsert_work

    merged = upsert_work(conn, client.work_by_id("W60"))
    assert merged == published
    assert resolve(conn, "doi", "10.48550/arxiv.2101.00001") == published


def test_update_adds_new_citers_of_known_papers_only(conn, client, fake, project):
    seed_both(conn, client, project)
    expand_project(conn, client, project)
    next_year = date.today().year + 1
    fake.works["W60"] = work(60, "Brand new readout paper", next_year, (1,), 0, "10.1000/new.60")
    fake.works["W61"] = work(61, "Old paper indexed late", 2015, (2,), 0, "10.1000/old.61")
    calls_before = len(fake.calls)

    report = update_project(conn, client, project)
    new_id = resolve(conn, "doi", "10.1000/new.60")
    assert report.new_papers == [new_id]
    assert resolve(conn, "doi", "10.1000/old.61") is None
    assert "W60" in members(conn, project, "frontier")
    seed = resolve(conn, "doi", "10.1109/seed.1")
    assert conn.execute("SELECT 1 FROM cites WHERE src = ? AND dst = ?", (new_id, seed)).fetchone()

    citer_calls = [
        dict(call.params)["filter"]
        for call in fake.calls[calls_before:]
        if "cites:" in dict(call.params).get("filter", "")
    ]
    assert len(citer_calls) == 1  # both seeds in one OR filter
    assert "from_publication_date:" in citer_calls[0]
    assert update_project(conn, client, project).new_papers == []


def test_update_needs_an_earlier_run(conn, client, project):
    seed_both(conn, client, project)
    with pytest.raises(BadInput, match="expand"):
        update_project(conn, client, project)


def test_update_stops_at_the_cap(conn, client, project):
    seed_both(conn, client, project)
    report = update_project(conn, client, project, since=date(2000, 1, 1), cap=1)
    assert report.citers_seen == 1
    assert report.capped


def test_each_known_paper_adds_its_best_neighbours_once(conn, client, project):
    seed_both(conn, client, project)
    assert pending_count(conn, project) == 2
    report = expand_project(conn, client, project, per_paper=2)
    frontier = members(conn, project, "frontier")
    assert len(frontier) == report.frontier_added <= 4
    assert pending_count(conn, project) == 0

    assert expand_project(conn, client, project, per_paper=2).frontier_added == 0
    accepted = resolve(conn, "openalex", sorted(frontier)[0])
    record_decision(conn, project, accepted, "accepted")
    assert pending_count(conn, project) == 1
    expand_project(conn, client, project, per_paper=2)
    assert len(members(conn, project, "frontier")) <= len(frontier) - 1 + 2
    assert pending_count(conn, project) == 0


def test_co_cited_papers_are_candidates(conn, client, project):
    seed_both(conn, client, project)
    expand_project(conn, client, project)
    assert "W30" in members(conn, project, "frontier")  # co-cited, never linked
    assert "W50" not in members(conn, project, "frontier")


def test_update_adds_at_most_the_limit_per_known_paper(conn, client, fake, project):
    seed_both(conn, client, project)
    expand_project(conn, client, project)
    next_year = date.today().year + 1
    for number in range(60, 70):
        fake.works[f"W{number}"] = work(number, f"New paper {number}", next_year, (1,), 0)
    report = update_project(conn, client, project, per_paper=3)
    assert report.citers_seen == 10
    assert len(report.new_papers) == 3
