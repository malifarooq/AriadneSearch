import httpx
import pytest

import coverage_audit
import loo
from conftest import FakeOpenAlex, make_client, small_field, work
from ariadnesearch.errors import BadInput
from ariadnesearch.identity import resolve
from ariadnesearch.ieee import BASE_URL, IEEEClient, open_store
from ariadnesearch.mine import MyPaperEntry, sync_published


def test_read_dois_skips_comments_and_normalizes(tmp_path):
    path = tmp_path / "dois.txt"
    path.write_text("# mine\n\nhttps://doi.org/10.1109/ABC\n10.1145/x\n", encoding="utf-8")
    assert coverage_audit.read_dois(path) == ["10.1109/abc", "10.1145/x"]


def test_audit_finds_missing_and_zero_reference_papers(client):
    report = coverage_audit.audit(
        ["10.1109/seed.1", "10.1000/ref.10", "10.1109/none"], client
    )
    assert report.total == 3
    assert report.missing == ["10.1109/none"]
    assert report.zero_references == ["10.1000/ref.10"]
    assert report.median_references == 3
    assert report.prefix_counts["10.1109"] == (1, 2)


def test_audit_compares_with_ieee_only_when_asked(conn, client, tmp_path, capsys):
    calls = []

    def handler(request):
        calls.append(request.url)
        doi = request.url.path.split("/articles/doi/", 1)[1]
        return httpx.Response(200, json={"articles": [{"doi": doi, "citing_paper_count": 20}]})

    report = coverage_audit.audit(["10.1109/seed.1"], client)
    assert report.ieee_compared == 0

    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    ieee = IEEEClient("key", open_store(tmp_path / "ieee.db"), conn, http=http, sleep=lambda _: None)
    report = coverage_audit.audit(["10.1109/seed.1", "10.1145/seed.2"], client, ieee)
    assert len(calls) == 1
    assert report.ieee_compared == 1
    assert report.median_openalex_to_ieee_ratio == 2.0

    coverage_audit.print_report(report)
    out = capsys.readouterr().out
    assert "median of 1: 2.00" in out
    assert "10.1109/seed.1" not in out


def test_holdout_split_is_stable_and_complete():
    ids = [f"p{i}" for i in range(10)]
    seeds, targets = loo.holdout_split(ids, 0.2, seed=1)
    assert len(targets) == 2
    assert sorted(seeds + targets) == sorted(ids)
    assert loo.holdout_split(ids, 0.2, seed=1) == (seeds, targets)
    with pytest.raises(BadInput):
        loo.holdout_split(ids, 1.5)


def test_recall_at_k():
    assert loo.recall_at_k(["a", "b", "c"], {"a", "c"}, k=2) == 0.5
    assert loo.recall_at_k(["a"], set()) == 0.0


def test_holdout_recovers_a_hidden_paper(conn, client, tmp_path):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{a, title={A}, doi={10.1109/seed.1}}\n"
        "@article{b, title={B}, doi={10.1145/seed.2}}\n"
        "@article{c, title={C}, doi={10.1000/citer.20}}\n"
        "@article{d, title={D}, doi={10.1000/citer.21}}\n"
        "@article{e, title={E}, doi={10.1000/citer.22}}\n",
        encoding="utf-8",
    )
    results = loo.run_holdout(conn, client, bib, fraction=0.2, k=10, seed=2)
    assert {item.method for item in results} == {"ranker", "citation count", "reachable"}
    by_method = {item.method: item for item in results}
    assert by_method["reachable"].recall == 1.0
    assert by_method["ranker"].recall == 1.0
    assert conn.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM membership").fetchone()[0] == 0


def test_excluded_survey_cannot_leak_its_references(conn, tmp_path):
    works = small_field() + [
        work(70, "A survey of everything", 2024, (1, 2, 20, 21), 90, "10.1000/survey.70")
    ]
    client = make_client(conn, FakeOpenAlex(works))
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{a, title={A}, doi={10.1109/seed.1}}\n"
        "@article{b, title={B}, doi={10.1145/seed.2}}\n",
        encoding="utf-8",
    )
    client.work_by_doi("10.1000/survey.70")
    from ariadnesearch.identity import upsert_work

    upsert_work(conn, client.work_by_doi("10.1000/survey.70"))
    survey = resolve(conn, "doi", "10.1000/survey.70")
    results = loo.run_holdout(
        conn, client, bib, 0.5, k=10, exclude_dois=("10.1000/survey.70",)
    )
    assert results[0].n_targets == 1
    assert survey


def test_temporal_needs_two_papers(conn, client):
    with pytest.raises(BadInput):
        loo.run_temporal(conn, client)


def test_temporal_excludes_the_newest_paper(conn):
    works = small_field() + [
        work(80, "My old paper", 2022, (10, 11, 30), 3, "10.1000/mine.old"),
        work(81, "My new paper", 2024, (10, 11, 40), 1, "10.1000/mine.new"),
    ]
    client = make_client(conn, FakeOpenAlex(works))
    sync_published(
        conn,
        client,
        [
            MyPaperEntry("Old", "10.1000/mine.old", "published"),
            MyPaperEntry("New", "10.1000/mine.new", "published"),
        ],
    )
    results = loo.run_temporal(conn, client, k=20)
    assert results[0].n_targets == 1
    assert conn.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 0
