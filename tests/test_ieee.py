import ast
from pathlib import Path

import httpx
import pytest

from ariadnesearch.config import REPO_ROOT
from ariadnesearch.db import usage_today
from ariadnesearch.errors import BudgetExceeded, MissingKey, UpstreamError
from ariadnesearch.ieee import BASE_URL, IEEEClient, is_ieee_doi, open_store, parse_article


def article(doi: str, number: int = 1, nested: bool = True) -> dict:
    data = {
        "doi": doi,
        "article_number": number,
        "title": "A paper",
        "citing_paper_count": 12,
        "citing_patent_count": 1,
        "abstract": "An abstract.",
    }
    if nested:
        data["index_terms"] = {"ieee_terms": {"terms": ["Qubit", "FPGA"]}}
    else:
        data["ieee_terms"] = ["Qubit", "FPGA"]
    return data


class FakeIEEE:
    def __init__(self):
        self.calls: list[httpx.URL] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url)
        path = request.url.path
        if "/articles/doi/" in path:
            dois = path.split("/articles/doi/", 1)[1].split(",")
            assert len(dois) <= 25
            return httpx.Response(200, json={"articles": [article(doi) for doi in dois]})
        return httpx.Response(200, json={"articles": [article("10.1109/found.1")]})


@pytest.fixture
def fake_ieee():
    return FakeIEEE()


def make_ieee(conn, tmp_path, handler, api_key="ieee-key", limit=180):
    store = open_store(tmp_path / "ieee.db")
    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return IEEEClient(api_key, store, conn, limit, http=http, sleep=lambda _: None)


@pytest.fixture
def ieee(conn, tmp_path, fake_ieee):
    return make_ieee(conn, tmp_path, fake_ieee)


def test_only_ieee_dois_qualify():
    assert is_ieee_doi("https://doi.org/10.1109/ABC.1")
    assert not is_ieee_doi("10.1145/abc.1")


@pytest.mark.parametrize("nested", [True, False])
def test_parse_article_reads_both_term_shapes(nested):
    record = parse_article(article("10.1109/ABC", 7, nested))
    assert record.doi == "10.1109/abc"
    assert record.article_number == "7"
    assert record.ieee_terms == ["Qubit", "FPGA"]


def test_article_without_doi_is_skipped():
    assert parse_article({"title": "No DOI"}) is None


def test_lookup_batches_25_dois_per_call(ieee, fake_ieee):
    dois = [f"10.1109/x.{i}" for i in range(26)]
    assert len(ieee.lookup_dois(dois)) == 26
    assert len(fake_ieee.calls) == 2


def test_lookup_skips_other_publishers_and_stored_records(ieee, fake_ieee):
    assert ieee.lookup_dois(["10.1145/not.ieee"]) == []
    assert fake_ieee.calls == []

    ieee.lookup_dois(["10.1109/x.1"])
    ieee.lookup_dois(["10.1109/x.1"])
    assert len(fake_ieee.calls) == 1


def test_refresh_skips_stored_records(ieee, fake_ieee):
    ieee.lookup_dois(["10.1109/x.1"])
    assert len(ieee.lookup_dois(["10.1109/x.1"], refresh=True)) == 1
    assert len(fake_ieee.calls) == 2


def test_cached_makes_no_call(ieee, fake_ieee):
    assert ieee.cached("10.1109/x.1") is None
    ieee.lookup_dois(["10.1109/x.1"])
    calls = len(fake_ieee.calls)
    assert ieee.cached("10.1109/x.1").citing_paper_count == 12
    assert len(fake_ieee.calls) == calls


def test_soft_limit_stops_calls(conn, tmp_path, fake_ieee):
    client = make_ieee(conn, tmp_path, fake_ieee, limit=1)
    client.search("qubit")
    with pytest.raises(BudgetExceeded):
        client.search("qubit")
    assert len(fake_ieee.calls) == 1


def test_call_count_survives_deleting_ieee_content(conn, tmp_path, fake_ieee):
    make_ieee(conn, tmp_path, fake_ieee).search("qubit")
    assert usage_today(conn, "ieee")[0] == 1


def test_failed_calls_are_counted(conn, tmp_path):
    client = make_ieee(conn, tmp_path, lambda _: httpx.Response(500, json={}))
    with pytest.raises(UpstreamError):
        client.search("qubit")
    assert usage_today(conn, "ieee")[0] == 1


def test_missing_key_is_reported_before_any_call(conn, tmp_path, fake_ieee):
    client = make_ieee(conn, tmp_path, fake_ieee, api_key=None)
    with pytest.raises(MissingKey):
        client.search("qubit")
    assert fake_ieee.calls == []


def test_errors_never_show_the_key(conn, tmp_path):
    def fail(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    client = make_ieee(conn, tmp_path, fail)
    with pytest.raises(UpstreamError) as error:
        client.search("qubit")
    assert "ieee-key" not in str(error.value)
    assert error.value.__cause__ is None


def test_ieee_content_stays_out_of_the_main_database(conn, ieee):
    ieee.lookup_dois(["10.1109/x.1"])
    tables = [
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    assert "ieee_record" not in tables
    assert conn.execute("SELECT COUNT(*) FROM raw_response").fetchone()[0] == 0
    assert ieee.store.execute("SELECT COUNT(*) FROM ieee_record").fetchone()[0] == 1


@pytest.mark.parametrize("name", ["rank", "expand", "mine", "views", "venues", "identity"])
def test_automatic_code_never_imports_ieee(name):
    tree = ast.parse(Path(REPO_ROOT / "ariadnesearch" / f"{name}.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module)
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not {"ariadnesearch.ieee", "ieee"} & modules
