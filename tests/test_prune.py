from conftest import FakeOpenAlex, make_client, work
from ariadnesearch.expand import add_member, add_seeds, hydrate
from ariadnesearch.identity import resolve, upsert_stubs, upsert_work
from ariadnesearch.prune import prune


def test_prune_removes_only_placeholders_that_can_never_be_suggested(conn, project):
    fake = FakeOpenAlex(
        [
            work(1, "My paper", doi="10.1109/mine", refs=(10,)),
            work(20, "A citing paper", refs=(1, 98, 99)),
            work(21, "Another citing paper", refs=(98,)),
        ]
    )
    client = make_client(conn, fake)
    add_seeds(conn, client, project, ["10.1109/mine"])
    for record in client.works_by_ids(["W20", "W21"]):
        upsert_work(conn, record)
    cited_by_me = upsert_stubs(conn, ["W77"])["W77"]
    conn.execute("INSERT INTO my_paper(id, title, status) VALUES (1, 'Draft', 'draft')")
    conn.execute("INSERT INTO my_citation VALUES (1, ?, 'aux')", (cited_by_me,))
    conn.commit()

    report = prune(conn)
    assert (report.papers, report.links) == (1, 1)
    assert resolve(conn, "openalex", "W99") is None  # cited once, by a paper in no project
    for kept in ("W10", "W98", "W77"):  # cited by my paper, cited twice, in a citation list
        assert resolve(conn, "openalex", kept) is not None

    # The citing paper joins the project: one list call restores its references.
    citer = resolve(conn, "openalex", "W20")
    add_member(conn, project, citer, "frontier")
    calls = len(fake.calls)
    hydrate(conn, client, [citer])
    assert resolve(conn, "openalex", "W99") is not None
    assert len(fake.calls) == calls + 1
    hydrate(conn, client, [citer])  # complete now, so nothing more is fetched
    assert len(fake.calls) == calls + 1


def test_pages_are_not_cached_and_old_entries_expire(conn, client):
    client.fetch_citers(["W1"], cap=5)
    assert conn.execute("SELECT COUNT(*) FROM cache.raw_response").fetchone()[0] == 0
    client.work_by_id("W1")
    assert conn.execute("SELECT COUNT(*) FROM cache.raw_response").fetchone()[0] == 1
    conn.execute("UPDATE cache.raw_response SET fetched_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    assert prune(conn).cache_entries == 1
