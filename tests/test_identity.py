import pytest

from ariadnesearch.db import create_project, init_db
from ariadnesearch.identity import (
    arxiv_doi,
    best_title_match,
    classify_pair,
    merge_works,
    normalize_doi,
    resolve,
    undo_merge,
    upsert_stubs,
    upsert_work,
)
from ariadnesearch.openalex import WorkRecord


@pytest.mark.parametrize(
    "raw",
    [
        "https://doi.org/10.1109/ABC.2024.123",
        "doi:10.1109/ABC.2024.123",
        "10.1109/abc.2024.123",
    ],
)
def test_normalize_doi(raw):
    assert normalize_doi(raw) == "10.1109/abc.2024.123"


def test_arxiv_doi_removes_version():
    assert arxiv_doi("1706.03762v5") == "10.48550/arxiv.1706.03762"


def test_preprint_and_published_paper_are_the_same():
    preprint = WorkRecord("W1", title="A Readout Method", year=2024, type="preprint")
    published = WorkRecord(
        "W2", title="A readout method", year=2025, type="article", venue="FPL"
    )
    assert classify_pair(preprint, published) == "same"


def test_journal_extension_stays_separate():
    conference = WorkRecord(
        "W1", title="A Readout Method", year=2024, type="article", venue="FPL"
    )
    journal = WorkRecord(
        "W2", title="A Readout Method", year=2025, type="article", venue="TRETS"
    )
    assert classify_pair(conference, journal) == "extension"


def test_merge_can_be_undone(tmp_path):
    conn = init_db(tmp_path / "test.db")
    keep = upsert_work(conn, WorkRecord("W1", doi="10.1109/keep"))
    drop = upsert_work(conn, WorkRecord("W2", doi="10.48550/arxiv.1234.5678"))

    merge_id = merge_works(conn, keep, drop, rule="preprint", confidence=1.0)
    assert resolve(conn, "doi", "10.48550/arxiv.1234.5678") == keep

    undo_merge(conn, merge_id)
    assert resolve(conn, "doi", "10.48550/arxiv.1234.5678") == drop


def test_generic_titles_are_never_merged():
    a = WorkRecord("W1", title="Editorial", year=2024, type="preprint")
    b = WorkRecord("W2", title="Editorial", year=2024, type="article")
    assert classify_pair(a, b) == "different"


def test_different_authors_keep_papers_separate():
    a = WorkRecord("W1", title="A Readout Method", type="preprint", authors=["Ada Lovelace"])
    b = WorkRecord("W2", title="A Readout Method", type="article", authors=["Alan Turing"])
    assert classify_pair(a, b) == "different"


def test_distant_years_keep_papers_separate():
    a = WorkRecord("W1", title="A Readout Method", year=2010, type="preprint")
    b = WorkRecord("W2", title="A Readout Method", year=2024, type="article")
    assert classify_pair(a, b) == "different"


def test_repository_copy_counts_as_a_preprint():
    a = WorkRecord("W1", title="A Readout Method", type="article", venue_type="repository", venue="arXiv")
    b = WorkRecord("W2", title="A Readout Method", type="article", venue="FPL")
    assert classify_pair(a, b) == "same"


def test_title_match_needs_a_close_title():
    records = [WorkRecord("W1", title="Quantum Readout on FPGAs")]
    assert best_title_match(records, "Quantum readout on FPGAs.") is records[0]
    assert best_title_match(records, "Readout") is None


def test_upsert_is_repeatable(tmp_path):
    conn = init_db(tmp_path / "test.db")
    record = WorkRecord("W1", doi="10.1/a", title="First title here", n_refs=2, referenced_ids=["W2", "W3"])
    first = upsert_work(conn, record)
    assert upsert_work(conn, record) == first
    assert conn.execute("SELECT COUNT(*) FROM cites").fetchone()[0] == 2


def test_stub_is_filled_in_place(tmp_path):
    conn = init_db(tmp_path / "test.db")
    stub = upsert_stubs(conn, ["W1"])["W1"]
    assert upsert_work(conn, WorkRecord("W1", title="Now known paper")) == stub
    row = conn.execute("SELECT title, level FROM work WHERE id = ?", (stub,)).fetchone()
    assert tuple(row) == ("Now known paper", "hydrated")


def test_preprint_and_published_records_merge_automatically(tmp_path):
    conn = init_db(tmp_path / "test.db")
    preprint = upsert_work(
        conn,
        WorkRecord("W1", doi="10.48550/arxiv.2401.00001", arxiv_id="2401.00001",
                   title="A Readout Method", year=2023, type="preprint",
                   venue="arXiv", venue_type="repository", cited_by_count=50),
    )
    published = upsert_work(
        conn,
        WorkRecord("W2", doi="10.1109/fpl.1", title="A Readout Method", year=2024,
                   type="article", venue="FPL", venue_type="conference", cited_by_count=5),
    )
    assert published != preprint
    assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 1
    assert resolve(conn, "arxiv", "2401.00001") == published
    assert resolve(conn, "openalex", "W1") == published


def test_journal_extension_is_linked_not_merged(tmp_path):
    conn = init_db(tmp_path / "test.db")
    conference = upsert_work(conn, WorkRecord("W1", title="A Readout Method", year=2023, type="article", venue="FPL"))
    journal = upsert_work(conn, WorkRecord("W2", title="A Readout Method", year=2024, type="article", venue="TRETS"))
    assert conference != journal
    row = conn.execute("SELECT a, b, kind FROM relation").fetchone()
    assert tuple(row) == (conference, journal, "extended_by")


def test_records_sharing_an_identifier_merge(tmp_path):
    conn = init_db(tmp_path / "test.db")
    first = upsert_work(conn, WorkRecord("W1", doi="10.1/a"))
    second = upsert_work(conn, WorkRecord("W2", arxiv_id="2401.00001"))
    upsert_work(conn, WorkRecord("W3", doi="10.1/a", arxiv_id="2401.00001"))
    assert resolve(conn, "openalex", "W1") == resolve(conn, "openalex", "W2")
    assert {first, second} >= {resolve(conn, "openalex", "W3")}


def test_merge_moves_everything_and_undo_restores_it(tmp_path):
    conn = init_db(tmp_path / "test.db")
    project = create_project(conn, "p")
    keep = upsert_work(conn, WorkRecord("W1", doi="10.1/keep", referenced_ids=["W9"], n_refs=1))
    drop = upsert_work(conn, WorkRecord("W2", doi="10.1/drop", referenced_ids=["W9", "W8"], n_refs=2))
    citer = upsert_work(conn, WorkRecord("W3", doi="10.1/citer", referenced_ids=["W2"], n_refs=1))
    conn.execute(
        "INSERT INTO membership(project_id, work_id, state, ts) VALUES (?, ?, 'frontier', 'x')",
        (project, keep),
    )
    conn.execute(
        "INSERT INTO membership(project_id, work_id, state, ts) VALUES (?, ?, 'accepted', 'x')",
        (project, drop),
    )
    before_edges = sorted(tuple(r) for r in conn.execute("SELECT src, dst FROM cites"))

    merge_id = merge_works(conn, keep, drop, rule="test", confidence=1.0)
    edges = {tuple(r) for r in conn.execute("SELECT src, dst FROM cites")}
    assert (citer, keep) in edges
    assert not any(drop in edge for edge in edges)
    state = conn.execute(
        "SELECT state FROM membership WHERE work_id = ?", (keep,)
    ).fetchone()["state"]
    assert state == "accepted"  # the stronger state wins

    undo_merge(conn, merge_id)
    assert sorted(tuple(r) for r in conn.execute("SELECT src, dst FROM cites")) == before_edges
    states = dict(tuple(r) for r in conn.execute("SELECT work_id, state FROM membership"))
    assert states == {keep: "frontier", drop: "accepted"}


def test_undo_cannot_run_twice(tmp_path):
    conn = init_db(tmp_path / "test.db")
    keep = upsert_work(conn, WorkRecord("W1"))
    drop = upsert_work(conn, WorkRecord("W2"))
    merge_id = merge_works(conn, keep, drop, "test", 1.0)
    undo_merge(conn, merge_id)
    with pytest.raises(KeyError):
        undo_merge(conn, merge_id)
