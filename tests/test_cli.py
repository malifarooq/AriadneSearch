import httpx
import pytest

from conftest import FakeOpenAlex, author_career, author_profiles, small_field, work
from ariadnesearch import cli
from ariadnesearch.ieee import BASE_URL as IEEE_URL
from ariadnesearch.ieee import IEEEClient
from ariadnesearch.openalex import BASE_URL, OpenAlexClient


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARIADNE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-openalex")
    monkeypatch.setenv("IEEE_API_KEY", "")
    fake = FakeOpenAlex(small_field() + author_career(), authors=author_profiles())

    def client(conn, api_key, daily_usd=1.0):
        http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(fake))
        return OpenAlexClient(conn, api_key, http=http, daily_usd=daily_usd, sleep=lambda _: None)

    monkeypatch.setattr(cli, "OpenAlexClient", client)

    def invoke(*args):
        code = cli.main(list(args))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


def test_init_is_repeatable(run):
    assert run("init")[0] == 0
    assert run("init")[0] == 0


def test_doctor_reports_setup_without_showing_keys(run):
    code, out, _ = run("doctor")
    assert code == 1  # the small test corpus lacks the check paper
    assert "OpenAlex key: set" in out
    assert "IEEE key:     NOT SET" in out
    assert "secret" not in out


def test_doctor_checks_paging_with_the_real_request(run, monkeypatch):
    target = [work(2741809807, "Test paper", refs=(1,))]
    citers = [work(i, f"Citer {i}", refs=(2741809807,), abstract={"Hi": [0]}) for i in range(10, 130)]
    fake = FakeOpenAlex(target + citers)

    def client(conn, api_key, daily_usd=1.0):
        http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(fake))
        return OpenAlexClient(conn, api_key, http=http, daily_usd=daily_usd, sleep=lambda _: None)

    monkeypatch.setattr(cli, "OpenAlexClient", client)
    code, out, _ = run("doctor")
    assert code == 0
    assert "paging working (101 of 120 citing papers, 101 with authors, 101 with abstracts)" in out
    assert sum(url.path == "/works" for url in fake.calls) == 2


def test_doctor_does_not_call_ieee_unless_asked(run, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("IEEE must not be called")

    monkeypatch.setattr(cli, "IEEEClient", forbidden)
    assert "IEEE:  " not in run("doctor")[1]


def test_doctor_ieee_check_uses_two_calls(run, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "/articles/doi/" in request.url.path:
            return httpx.Response(200, json={"articles": [{
                "doi": "10.1109/x.1", "citing_paper_count": 7,
                "index_terms": {"ieee_terms": {"terms": ["Qubit"]}}}]})
        return httpx.Response(200, json={"articles": [{"doi": "10.1049/other"}, {"doi": "10.1109/x.1"}]})

    def ieee_client(api_key, store, conn, **kwargs):
        http = httpx.Client(base_url=IEEE_URL, transport=httpx.MockTransport(handler))
        return IEEEClient("key", store, conn, http=http, sleep=lambda _: None)

    monkeypatch.setattr(cli, "IEEEClient", ieee_client)
    out = run("doctor", "--ieee")[1]
    assert len(calls) == 2
    assert "working (7 citations and 1 IEEE terms" in out


def test_full_workflow(run, tmp_path):
    assert run("project", "readout")[0] == 0
    assert run("seed", "readout", "10.1109/seed.1", "10.1145/seed.2", "10.9999/none")[1].startswith(
        "added 2 seeds"
    )
    code, out, _ = run("expand", "readout")
    assert code == 0
    assert "added" in out

    csv_path = tmp_path / "out.csv"
    code, out, _ = run("rank", "readout", "-k", "5", "--csv", str(csv_path))
    assert code == 0
    assert "5 shown" in out
    assert csv_path.read_text(encoding="utf-8").startswith("score,id,title")

    paper = out.splitlines()[2].split()[1]  # after the count and header lines
    assert run("decide", "readout", paper, "accepted")[0] == 0

    bib = tmp_path / "out.bib"
    assert run("export", "readout", "--out", str(bib))[0] == 0
    assert "10.1109/seed.1" in bib.read_text(encoding="utf-8")


def test_update_lists_new_papers(run):
    run("project", "readout")
    run("seed", "readout", "10.1109/seed.1", "10.1145/seed.2")
    assert run("update", "readout")[0] == 1  # needs an expand first
    run("expand", "readout")
    code, out, _ = run("update", "readout", "--since", "2000-01-01")
    assert code == 0
    assert "new to this project" in out


def test_citers_adds_citing_papers(run):
    run("project", "readout")
    run("seed", "readout", "10.1109/seed.1")
    code, out, _ = run("citers", "readout", "10.1109/seed.1", "--add", "2")
    assert code == 0
    assert "3 papers cite it" in out and "Added 2 to review" in out
    assert run("citers", "readout", "10.9999/none")[0] == 1


def test_decide_accepts_a_doi(run):
    run("project", "p")
    run("seed", "p", "10.1109/seed.1")
    assert run("decide", "p", "10.1109/seed.1", "read")[0] == 0


def test_seed_needs_input(run):
    run("project", "p")
    assert run("seed", "p")[0] == 2


def test_unknown_project_is_a_clean_error(run):
    code, _, err = run("rank", "missing")
    assert code == 1
    assert err == "error: No project named: missing\n"


def test_duplicate_project_is_a_clean_error(run):
    run("project", "p")
    code, _, err = run("project", "p")
    assert code == 1
    assert "already exists" in err


def test_missing_file_is_a_clean_error(run, tmp_path):
    code, _, err = run("mine", "sync", "--file", str(tmp_path / "none.yaml"))
    assert code == 1
    assert err.startswith("error:")


def test_mine_and_seed_from_my_citations(run, tmp_path):
    papers = tmp_path / "mine.yaml"
    papers.write_text(
        "papers:\n  - title: Mine\n    doi: 10.1109/seed.1\n", encoding="utf-8"
    )
    assert "stored 3" in run("mine", "sync", "--file", str(papers))[1]
    run("project", "p")
    assert run("seed", "p", "--mine")[1].startswith("added 3 seeds")


def test_venue_commands(run, tmp_path):
    csv_path = tmp_path / "core.csv"
    csv_path.write_text("Title,Acronym,Source,Rank\nTest Journal,TJ,ICORE2026,A\n")
    assert "imported 1" in run("venue", "import", str(csv_path))[1]

    run("project", "p")
    run("seed", "p", "10.1109/seed.1")
    assert "no unmatched" in run("venue", "unmatched", "p")[1]
    assert run("venue", "map", "Odd Name", "Test Journal")[0] == 0
    assert run("venue", "map", "Odd Name", "No Such Title")[0] == 1
    assert "added" in run("venue", "add", "p", "S1", "--since", "2020")[1]


def test_author_commands(run):
    code, out, _ = run("author", "find", "Grace Hopper")
    assert code == 0 and out.startswith("A1 ")
    code, out, _ = run("author", "new", "A1")
    assert code == 0 and "Created the author project Grace Hopper" in out
    assert "5 of their papers" in out
    code, out, _ = run("author", "report", "Grace Hopper")
    assert code == 0
    assert "Notes on the analytical engine" in out
    assert "The first compiler" in out
    code, out, _ = run("update", "Grace Hopper")
    assert code == 0 and "0 new" in out
