import httpx
import pytest

from conftest import FakeOpenAlex, make_client, work
from ariadnesearch.db import usage_today
from ariadnesearch.errors import BudgetExceeded, MissingKey, UpstreamError
from ariadnesearch.openalex import decode_abstract, parse_work, short_id


def test_decode_abstract_restores_word_order():
    index = {"qubit": [1], "A": [0], "readout": [2], "method": [3]}
    assert decode_abstract(index) == "A qubit readout method"
    assert decode_abstract(None) is None


def test_parse_work_flattens_a_response():
    raw = work(
        7,
        "A Title",
        2024,
        (1, 2),
        cited_by=5,
        doi="10.1109/ABC.1",
        venue=("S9", "Conf", "conference"),
        authors=("Ada Lovelace", "Alan Turing"),
        percentile=0.8,
        abstract={"Hello": [0], "world": [1]},
    )
    record = parse_work(raw)
    assert record.openalex_id == "W7"
    assert record.doi == "10.1109/abc.1"
    assert record.venue == "Conf"
    assert record.venue_id == "S9"
    assert record.venue_type == "conference"
    assert record.authors == ["Ada Lovelace", "Alan Turing"]
    assert record.percentile == 0.8
    assert record.referenced_ids == ["W1", "W2"]
    assert record.n_refs == 2
    assert record.abstract == "Hello world"


def test_parse_work_handles_missing_location_and_percentile():
    record = parse_work(work(8, "No venue", venue=None, percentile=None))
    assert record.venue is None
    assert record.venue_id is None
    assert record.percentile is None


def test_parse_work_finds_arxiv_id_in_doi_and_locations():
    assert parse_work(work(1, "T", doi="10.48550/arXiv.2401.00001")).arxiv_id == "2401.00001"
    assert parse_work(work(2, "T", arxiv="2402.12345v2")).arxiv_id == "2402.12345"


def test_short_id():
    assert short_id("https://openalex.org/W123") == "W123"
    assert short_id("W123") == "W123"


def test_cached_response_makes_no_second_call(conn, client, fake):
    assert client.work_by_doi("10.1109/seed.1").title
    assert client.work_by_doi("https://doi.org/10.1109/SEED.1").title
    assert len(fake.calls) == 1


def test_unknown_doi_returns_none(client):
    assert client.work_by_doi("10.9999/missing") is None


def test_batches_respect_the_or_limit(conn):
    works = [work(i, f"Paper {i}") for i in range(1, 251)]
    fake = FakeOpenAlex(works)
    client = make_client(conn, fake)
    records = client.works_by_ids([f"W{i}" for i in range(1, 251)])
    assert len(records) == 250
    assert len(fake.calls) == 3


def test_doi_with_filter_syntax_is_fetched_singly(conn):
    fake = FakeOpenAlex([work(1, "Odd", doi="10.1000/a,b"), work(2, "Plain", doi="10.1000/c")])
    client = make_client(conn, fake)
    records = client.works_by_dois(["10.1000/a,b", "10.1000/c"])
    assert {record.openalex_id for record in records} == {"W1", "W2"}


def test_citers_follow_cursor_pages_up_to_the_cap(conn):
    works = [work(1, "Target")] + [work(i, f"Citer {i}", refs=(1,)) for i in range(2, 353)]
    fake = FakeOpenAlex(works)
    client = make_client(conn, fake)

    records, total = client.fetch_citers(["W1"], cap=250)
    assert total == 351
    assert len(records) == 250
    assert len(fake.calls) == 3


def test_spending_is_tracked_from_response_metadata(conn, client):
    client.works_by_ids(["W1"])
    calls, cost = usage_today(conn, "openalex")
    assert calls == 1
    assert cost == pytest.approx(0.0001)


def test_budget_stops_further_calls(conn, fake):
    client = make_client(conn, fake, daily_usd=0.0001)
    client.works_by_ids(["W1"])
    with pytest.raises(BudgetExceeded):
        client.works_by_ids(["W2"])
    assert len(fake.calls) == 1


def test_retries_rate_limits_and_server_errors(conn):
    responses = iter(
        [
            httpx.Response(429, json={}),
            httpx.Response(500, json={}),
            httpx.Response(200, json=work(1, "Done")),
        ]
    )
    client = make_client(conn, lambda _: next(responses))
    assert client.work_by_id("W1").title == "Done"


def test_network_errors_are_retried_then_reported(conn):
    def fail(request):
        raise httpx.ConnectError("down", request=request)

    client = make_client(conn, fail)
    with pytest.raises(UpstreamError):
        client.work_by_id("W1")


def test_exhausted_daily_budget_stops_immediately(conn):
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, headers={"X-RateLimit-Remaining": "0"}, json={})

    client = make_client(conn, limited)
    with pytest.raises(BudgetExceeded):
        client.work_by_id("W1")
    assert len(calls) == 1


def test_api_key_is_sent_but_never_stored_or_shown(conn):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=work(1, "T"))

    client = make_client(conn, handler)
    client.work_by_id("W1")
    assert "api_key=test-key" in urls[0]
    stored = conn.execute("SELECT url FROM raw_response").fetchone()["url"]
    assert "test-key" not in stored

    rejecting = make_client(conn, lambda _: httpx.Response(401, json={}))
    with pytest.raises(UpstreamError) as error:
        rejecting.work_by_id("W2")
    assert "test-key" not in str(error.value)


def test_keyless_paging_asks_for_a_key(conn):
    body = {"error": "API key required for cursor paging"}
    client = make_client(conn, lambda _: httpx.Response(401, json=body), api_key=None)
    with pytest.raises(MissingKey) as error:
        client.fetch_citers(["W1"], cap=5)
    assert "OPENALEX_API_KEY" in str(error.value)


def test_requests_sort_the_way_the_live_api_accepts(conn, fake, client):
    client.fetch_citers(["W1"], cap=5)
    client.works_by_source("S1", 2000, cap=5)
    assert dict(fake.calls[0].params)["sort"] == "publication_date:desc"
    assert dict(fake.calls[1].params)["sort"] == "cited_by_count:desc"


def test_rejected_requests_explain_why_without_the_key(conn):
    client = make_client(
        conn, lambda _: httpx.Response(400, json={"message": "bad value test-key"})
    )
    with pytest.raises(UpstreamError) as error:
        client.work_by_id("W1")
    assert "bad value" in str(error.value)
    assert "test-key" not in str(error.value)


def test_expired_cache_is_refetched(conn, fake):
    client = make_client(conn, fake, max_age_days=0)
    client.work_by_id("W1")
    client.work_by_id("W1")
    assert len(fake.calls) == 2
