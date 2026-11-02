from conftest import FakeOpenAlex, make_client, work
from ariadnesearch.expand import add_seeds
from ariadnesearch.topics import refresh_topics
from ariadnesearch.views import authors_view

READOUT = ("Qubit readout", "Atomic and Molecular Physics", "Physics and Astronomy")


def test_topics_are_stored_and_backfilled_once(conn, project):
    fake = FakeOpenAlex(
        [
            work(1, "With a topic", doi="10.1109/t.1", topic=READOUT),
            work(2, "Without one", doi="10.1109/t.2"),
        ]
    )
    client = make_client(conn, fake)
    first, second = add_seeds(conn, client, project, ["10.1109/t.1", "10.1109/t.2"]).added
    row = conn.execute("SELECT topic, subfield FROM work WHERE id = ?", (first,)).fetchone()
    assert tuple(row) == READOUT[:2]

    conn.execute("UPDATE work SET topic = NULL, subfield = NULL WHERE id = ?", (first,))
    assert refresh_topics(conn, client, [first, second]) == 2
    assert conn.execute("SELECT topic FROM work WHERE id = ?", (first,)).fetchone()[0] == READOUT[0]
    assert conn.execute("SELECT topic FROM work WHERE id = ?", (second,)).fetchone()[0] == ""
    calls = len(fake.calls)
    assert refresh_topics(conn, client, [first, second]) == 0
    assert len(fake.calls) == calls


def test_authors_view_ranks_authors_of_your_papers_first(conn, project):
    fake = FakeOpenAlex(
        [
            work(1, "Mine", doi="10.1109/a.1", authors=("Ada Lovelace", "Alan Turing")),
            work(2, "Also mine", doi="10.1109/a.2", authors=("Ada Lovelace",)),
            work(3, "Once", doi="10.1109/a.3", authors=("Grace Hopper",)),
        ]
    )
    client = make_client(conn, fake)
    ids = add_seeds(conn, client, project, ["10.1109/a.1", "10.1109/a.2", "10.1109/a.3"]).added
    items = authors_view(conn, project)["items"]
    assert [item["name"] for item in items] == ["Ada Lovelace"]  # one-paper authors are left out
    assert items[0]["added"] == 2
    assert set(items[0]["work_ids"]) == set(ids[:2])
