from conftest import FakeOpenAlex, make_client, work
from ariadnesearch.expand import add_seeds
from ariadnesearch.journals import impact_for_works, refresh_impact

STATS = {"id": "https://openalex.org/S9", "summary_stats": {"2yr_mean_citedness": 4.08, "h_index": 260}}


def test_journal_impact_is_fetched_once_and_only_for_journals(conn, project):
    fake = FakeOpenAlex(
        [
            work(1, "Journal paper", doi="10.1109/j.1", venue=("S9", "IEEE Transactions on Computers", "journal")),
            work(2, "Conference paper", doi="10.1109/c.1", venue=("S8", "FPL", "conference")),
        ],
        sources=[STATS],
    )
    client = make_client(conn, fake)
    journal, conference = add_seeds(conn, client, project, ["10.1109/j.1", "10.1109/c.1"]).added

    assert refresh_impact(conn, client, [journal, conference]) == 1
    assert dict(fake.calls[-1].params)["filter"] == "openalex:S9"
    calls = len(fake.calls)
    assert refresh_impact(conn, client, [journal, conference]) == 0
    assert len(fake.calls) == calls

    impact = impact_for_works(conn, [journal, conference])
    assert impact[journal].mean_citedness == 4.08
    assert impact[journal].h_index == 260
    assert conference not in impact
