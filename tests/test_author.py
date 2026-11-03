import pytest

from conftest import FakeOpenAlex, author_career, author_profiles, make_client
from ariadnesearch.author import (
    author_context,
    author_view,
    create_author_project,
    find_authors,
    update_author,
)
from ariadnesearch.db import is_author_project, usage_today
from ariadnesearch.errors import BadInput
from ariadnesearch.expand import expand_project, pending_count, record_decision
from ariadnesearch.tune import tune_project

@pytest.fixture
def people():
    return FakeOpenAlex(author_career(), authors=author_profiles())


@pytest.fixture
def people_client(conn, people):
    return make_client(conn, people)


def test_name_search_puts_full_name_matches_first(people_client):
    found = find_authors(people_client, "Grace Hopper")
    assert [profile.id for profile in found][:2] == ["A1", "A9"]  # full matches, larger first
    assert found[0].institutions == ["Test University"]
    assert found[0].first_year == 2015


def test_orcid_and_openalex_ids_are_free_lookups(conn, people_client, people):
    assert [p.id for p in find_authors(people_client, "https://orcid.org/0000-0002-1825-0097")] == ["A1"]
    assert [p.id for p in find_authors(people_client, "https://openalex.org/a2")] == ["A2"]
    assert usage_today(conn, "openalex")[1] == 0
    assert all("search" not in str(call) for call in people.calls)


def test_creating_an_author_project_fetches_their_papers(conn, people_client):
    project_id, report = create_author_project(conn, people_client, ["A1"])
    assert is_author_project(conn, project_id)
    name = conn.execute("SELECT name FROM project WHERE id = ?", (project_id,)).fetchone()[0]
    assert name == "Grace Hopper"
    seeds = conn.execute(
        "SELECT w.title FROM membership m JOIN work w ON w.id = m.work_id "
        "WHERE m.project_id = ? AND m.state = 'seed' ORDER BY w.year",
        (project_id,),
    ).fetchall()
    assert [row[0] for row in seeds] == [
        "The first compiler",  # cited by her and listing her, though not in her profile
        "Compilers for everyone",
        "Compilers that read English",
        "Debugging moths at scale",
        "Nanoseconds made visible",
    ]  # the paratext record is left out
    assert report.papers == 5 and len(report.new_papers) == 4 and report.claimed == 1
    assert pending_count(conn, project_id) == 0


def test_cited_papers_are_described_but_stay_placeholders(conn, people_client):
    create_author_project(conn, people_client, ["A1"])
    row = conn.execute(
        "SELECT w.title, w.authors, w.level FROM work w "
        "JOIN work_alias a ON a.work_id = w.id WHERE a.value = 'W205'"
    ).fetchone()
    assert row["title"] == "Abstraction in programming"
    assert "Barbara Liskov" in row["authors"]
    assert row["level"] == "stub"  # its own references are fetched only if it joins a project


def test_most_cited_papers_join_as_inspirations(conn, people_client):
    project_id, _ = create_author_project(conn, people_client, ["A1"])
    frontier = {
        row[0]
        for row in conn.execute(
            "SELECT a.value FROM membership m JOIN work_alias a ON a.work_id = m.work_id "
            "WHERE m.project_id = ? AND m.state = 'frontier' AND a.scheme = 'openalex'",
            (project_id,),
        )
    }
    assert {"W201", "W205", "W206"} <= frontier
    assert "W300" not in frontier  # it lists her, so it is one of her papers
    assert "W101" not in frontier


def test_author_view(conn, people_client):
    project_id, _ = create_author_project(conn, people_client, ["A1"])
    view = author_view(conn, project_id)
    assert view["name"] == "Grace Hopper"
    assert (view["papers"], view["first_year"], view["last_year"]) == (5, 2014, 2020)
    assert [row["papers"] for row in view["per_year"]] == [1, 1, 1, 0, 0, 1, 1]
    assert view["self_links"] == 3  # 102 cites 101, 104 cites 102, 103 cites 300
    assert [item["title"] for item in view["claimed"]] == ["The first compiler"]

    top = view["inspirations"][0]
    assert top["title"] == "Notes on the analytical engine"
    assert (top["count"], top["first_year"], top["last_year"]) == (3, 2015, 2019)
    assert top["state"] == "frontier"
    titles = {item["title"] for item in view["inspirations"]}
    assert "The first compiler" not in titles
    assert view["missing_own"] == []

    coauthors = {person["name"]: person for person in view["coauthors"]}
    assert set(coauthors) == {"Alan Turing", "Barbara Liskov"}
    assert (coauthors["Alan Turing"]["papers"], coauthors["Alan Turing"]["last_year"]) == (2, 2016)
    cited = {person["name"]: person for person in view["inspiring_authors"]}
    assert "Grace Hopper" not in cited
    assert cited["Ada Lovelace"]["papers"] == 3
    assert cited["Alan Turing"]["coauthor"] is True


def test_author_context_for_their_paper_and_a_cited_one(conn, people_client):
    project_id, _ = create_author_project(conn, people_client, ["A1"])
    ids = dict(conn.execute("SELECT value, work_id FROM work_alias WHERE scheme = 'openalex'"))
    paper = author_context(conn, project_id, ids["W102"])
    assert paper["claimed"] is False
    assert author_context(conn, project_id, ids["W300"])["claimed"] is True
    assert [item["title"] for item in paper["builds_on"]] == ["Compilers for everyone"]
    assert [item["title"] for item in paper["built_on_by"]] == ["Nanoseconds made visible"]
    assert [item["title"] for item in paper["new_here"]] == [
        "The mathematical theory of communication"
    ]  # 201 was first cited in 2015

    later = author_context(conn, project_id, ids["W103"])
    assert {item["title"] for item in later["new_here"]} == {
        "Abstraction in programming",
        "Structured programming notes",
    }
    assert [item["title"] for item in later["builds_on"]] == ["The first compiler"]
    cited = author_context(conn, project_id, ids["W201"])
    assert cited["kind"] == "cited"
    assert [item["year"] for item in cited["papers"]] == [2015, 2016, 2019]


def test_updating_adds_only_new_papers(conn, people_client):
    project_id, _ = create_author_project(conn, people_client, ["A1"])
    report = update_author(conn, people_client, project_id)
    assert report.new_papers == [] and report.claimed == 0 and report.papers == 5


def test_a_second_profile_adds_its_papers(conn, people_client):
    project_id, report = create_author_project(conn, people_client, ["A1", "A2"])
    assert report.papers == 6
    view = author_view(conn, project_id)
    assert view["name"] == "Grace Hopper"  # the profile with more papers
    assert "G. Hopper" not in {person["name"] for person in view["coauthors"]}


def test_claimed_papers_are_hydrated_and_can_be_marked_not_theirs(conn, people_client):
    project_id, _ = create_author_project(conn, people_client, ["A1"])
    claimed = author_view(conn, project_id)["claimed"][0]["id"]
    level = conn.execute("SELECT level FROM work WHERE id = ?", (claimed,)).fetchone()[0]
    assert level == "hydrated"

    record_decision(conn, project_id, claimed, "rejected")  # a namesake's paper
    assert update_author(conn, people_client, project_id).claimed == 0
    view = author_view(conn, project_id)
    assert view["claimed"] == [] and view["papers"] == 4
    rejected = next(item for item in view["inspirations"] if item["id"] == claimed)
    assert rejected["state"] == "rejected"  # now an ordinary cited paper


def test_update_graph_never_fetches_citers_of_their_papers(conn, people_client, people):
    project_id, _ = create_author_project(conn, people_client, ["A1"])
    people.calls.clear()
    expand_project(conn, people_client, project_id)
    assert not any("cites" in str(call) for call in people.calls)


def test_tuning_refuses_author_projects(conn, people_client):
    project_id, _ = create_author_project(conn, people_client, ["A1"])
    with pytest.raises(BadInput, match="topic project"):
        tune_project(conn, people_client, project_id)


def test_unknown_or_malformed_profiles_are_refused(conn, people_client):
    with pytest.raises(BadInput):
        create_author_project(conn, people_client, ["not-an-id"])
    with pytest.raises(BadInput):
        create_author_project(conn, people_client, [" "])
