from pathlib import Path

import pytest

from conftest import FakeOpenAlex, make_client, work
from ariadnesearch.errors import BadInput, NotFound, UpstreamError
from ariadnesearch.expand import add_seeds, expand_project
from ariadnesearch.identity import resolve
from ariadnesearch.rank import Weights, load_snapshot, rank
from ariadnesearch.venues import (
    TARGET_EDITION,
    download_core_csv,
    VenueRank,
    add_target,
    load_targets,
    set_targets,
    add_venue_papers,
    find_venues,
    import_core_csv,
    match_venue,
    meets_minimum,
    rank_for,
    ranks_for_works,
    set_venue_alias,
    unmatched_venues,
)

NO_HEADER = (
    '"1","International Conference on Field Programmable Logic and Applications",'
    '"FPL","ICORE2026","A","No","4606","","",""\n'
    '"2","ACM/SIGDA International Symposium on Field-Programmable Gate Arrays",'
    '"FPGA","ICORE2026","A","No","4606","","",""\n'
    '"3","International Conference on Communications","ICC","ICORE2026","B","No","4006","","",""\n'
    '"4","IEEE International Conference on Communications in China","ICCC","ICORE2026","C","No","4006","","",""\n'
    '"5","Very Local Workshop on Things","VLWT","ICORE2026","Other","No","4606","","",""\n'
)
WITH_HEADER = (
    "Title,Acronym,Source,Rank\n"
    "International Conference on Field Programmable Logic and Applications,FPL,CORE2023,B\n"
)


@pytest.fixture
def ranked(conn, tmp_path):
    path = tmp_path / "core.csv"
    path.write_text(NO_HEADER, encoding="utf-8")
    assert import_core_csv(conn, path) == 5
    return conn


def write_csv(tmp_path: Path, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / "export.csv"
    path.write_text(text, encoding=encoding)
    return path


def test_import_detects_columns_without_a_header(ranked):
    row = ranked.execute(
        "SELECT * FROM venue_rank WHERE acronym = 'FPL'"
    ).fetchone()
    assert row["title"].startswith("International Conference on Field")
    assert row["rank"] == "A"
    assert row["edition"] == "ICORE2026"


def test_import_reads_named_columns(conn, tmp_path):
    assert import_core_csv(conn, write_csv(tmp_path, WITH_HEADER)) == 1
    row = conn.execute("SELECT edition, rank, acronym FROM venue_rank").fetchone()
    assert tuple(row) == ("CORE2023", "B", "FPL")


def test_import_handles_a_byte_order_mark_and_latin1(conn, tmp_path):
    assert import_core_csv(conn, write_csv(tmp_path, WITH_HEADER, "utf-8-sig")) == 1
    text = "Title,Acronym,Source,Rank\nConf\xe9rence Fran\xe7aise,CF,ICORE2026,C\n"
    assert import_core_csv(conn, write_csv(tmp_path, text, "latin-1")) == 1


def test_import_needs_an_edition(conn, tmp_path):
    text = '"1","A Long Conference Title Here","ALCTH","A"\n"2","Another Conference Name","ACN","B"\n'
    path = write_csv(tmp_path, text)
    with pytest.raises(BadInput, match="edition"):
        import_core_csv(conn, path)
    assert import_core_csv(conn, path, "ICORE2026") == 2


def test_import_refuses_an_unknown_layout(conn, tmp_path):
    with pytest.raises(BadInput, match="rank column"):
        import_core_csv(conn, write_csv(tmp_path, "a,b,c\nd,e,f\n"))


@pytest.mark.parametrize(
    "raw, acronym",
    [
        ("2024 34th International Conference on Field-Programmable Logic and Applications (FPL)", "FPL"),
        ("International Conference on Field Programmable Logic and Applications", "FPL"),
        ("Proceedings of the 2024 ACM/SIGDA International Symposium on Field Programmable Gate Arrays", "FPGA"),
        ("FPL", "FPL"),
    ],
)
def test_match_venue_finds_year_specific_names(ranked, raw, acronym):
    title = match_venue(ranked, raw)
    assert ranked.execute(
        "SELECT acronym FROM venue_rank WHERE title = ?", (title,)
    ).fetchone()["acronym"] == acronym


@pytest.mark.parametrize(
    "raw",
    [
        "Physical Review Applied",
        "Nature",
        "IEEE Transactions on Very Large Scale Integration Systems",
    ],
)
def test_match_venue_leaves_other_venues_unknown(ranked, raw):
    assert match_venue(ranked, raw) is None


REAL_TITLES = (
    "Title,Acronym,Source,Rank\n"
    "Design Automation and Test in Europe Conference,DATE,ICORE2026,A\n"
    "Advances in Neural Information Processing Systems (was NIPS),NeurIPS,ICORE2026,A*\n"
    "IEEE Conference on Computer Vision and Pattern Recognition,CVPR,ICORE2026,A*\n"
    "IEEE International Symposium on Circuits and Systems,ISCAS,ICORE2026,C\n"
    "International Conference on Advanced Computing and Communications,ADCOM,ICORE2026,National: India\n"
)


@pytest.mark.parametrize(
    "raw, title_start",
    [
        ("Design, Automation, and Test in Europe", "Design Automation"),
        ("neural information processing systems", "Advances in Neural"),
        (
            "IEEE Computer Society Conference on Computer Vision and Pattern Recognition/"
            "IEEE Computer Society Conference on Computer Vision and Pattern Recognition",
            "IEEE Conference on Computer Vision",
        ),
        ("IEEE International Midwest Symposium on Circuits and Systems (MWSCAS)", None),
        ("IEEE International Conference on Edge Computing and Communications (EDGE)", None),
    ],
)
def test_word_match_finds_variants_but_not_neighbours(conn, tmp_path, raw, title_start):
    import_core_csv(conn, write_csv(tmp_path, REAL_TITLES))
    title = match_venue(conn, raw)
    assert (title or "").startswith(title_start) if title_start else title is None


def test_journals_are_not_word_matched_to_conferences(conn, tmp_path):
    import_core_csv(conn, write_csv(tmp_path, REAL_TITLES))
    raw = "Computer Vision and Pattern Recognition"
    assert match_venue(conn, raw, venue_type="journal") is None
    assert match_venue(conn, raw + " Conference", venue_type="conference") is not None


def test_acronym_needs_a_similar_title(ranked):
    assert match_venue(ranked, "International Cycling Championship (ICC)") is None
    assert match_venue(ranked, "2024 IEEE Conference on Communications (ICC)") is not None


def test_repositories_are_never_ranked(ranked):
    assert rank_for(ranked, "FPL", venue_type="repository") is None


def test_rank_uses_the_newest_edition_unless_told_otherwise(ranked, tmp_path):
    import_core_csv(ranked, write_csv(tmp_path, WITH_HEADER))
    raw = "International Conference on Field Programmable Logic and Applications"
    assert rank_for(ranked, raw) == VenueRank("A", "ICORE2026")
    assert rank_for(ranked, raw, edition="CORE2023") == VenueRank("B", "CORE2023")


def test_unknown_rank_is_none_not_low(ranked):
    assert rank_for(ranked, None) is None
    assert rank_for(ranked, "Physical Review Applied") is None
    assert not meets_minimum(None, "C")
    assert meets_minimum(None, None)


def test_minimum_rank_filter():
    assert meets_minimum(VenueRank("A*", "x"), "A")
    assert meets_minimum(VenueRank("A", "x"), "A")
    assert not meets_minimum(VenueRank("B", "x"), "A")
    assert not meets_minimum(VenueRank("Other", "x"), "C")


def test_manual_match_survives_reimport(ranked, tmp_path):
    raw = "A Venue With An Odd Name"
    assert match_venue(ranked, raw) is None
    set_venue_alias(ranked, raw, "Very Local Workshop on Things")
    assert rank_for(ranked, raw).rank == "Other"

    path = write_csv(tmp_path, NO_HEADER)
    import_core_csv(ranked, path)
    assert rank_for(ranked, raw).rank == "Other"


def test_manual_match_needs_a_real_title(ranked):
    with pytest.raises(NotFound):
        set_venue_alias(ranked, "Anything", "No Such Conference")


def test_new_import_rechecks_unmatched_venues(conn, tmp_path):
    raw = "International Conference on Field Programmable Logic and Applications"
    import_core_csv(
        conn,
        write_csv(tmp_path, "Title,Acronym,Source,Rank\nOther Conference Name,OCN,ICORE2026,B\n"),
    )
    assert match_venue(conn, raw) is None
    import_core_csv(conn, write_csv(tmp_path, WITH_HEADER))
    assert match_venue(conn, raw) is not None


def test_ranks_for_works_and_unmatched(ranked, project):
    fake = FakeOpenAlex(
        [
            work(1, "Conference paper", doi="10.1000/a",
                 venue=("S1", "2024 34th International Conference on Field-Programmable "
                              "Logic and Applications (FPL)", "conference")),
            work(2, "Journal paper", doi="10.1000/b",
                 venue=("S2", "Physical Review Applied", "journal")),
            work(3, "Preprint", doi="10.1000/c", venue=("S3", "arXiv", "repository")),
        ]
    )
    client = make_client(ranked, fake)
    add_seeds(ranked, client, project, ["10.1000/a", "10.1000/b", "10.1000/c"])

    ids = [resolve(ranked, "doi", doi) for doi in ("10.1000/a", "10.1000/b", "10.1000/c")]
    ranks = ranks_for_works(ranked, ids)
    assert ranks[ids[0]].rank == "A"
    assert ids[1] not in ranks
    assert ids[2] not in ranks
    assert unmatched_venues(ranked, project) == ["Physical Review Applied"]


def test_core_rank_reaches_the_score(conn, client, project, tmp_path):
    add_seeds(conn, client, project, ["10.1109/seed.1", "10.1145/seed.2"])
    expand_project(conn, client, project)

    def scores(weights):
        snapshot, state = load_snapshot(conn, project)
        return {r.work_id: r.score for r in rank(snapshot, state, weights)}

    before = scores(Weights(venue=0.2))
    import_core_csv(conn, write_csv(tmp_path, "Title,Acronym,Source,Rank\nTest Journal,TJ,ICORE2026,A*\n"))
    after = scores(Weights(venue=0.2))
    assert all(after[key] == pytest.approx(before[key] + 0.1) for key in before)
    assert scores(Weights(venue=0)) == before


def test_find_venues_expands_a_core_acronym(ranked):
    fake = FakeOpenAlex(
        [],
        sources=[{"id": "https://openalex.org/S5", "display_name": "FPL", "type": "conference", "works_count": 900}],
    )
    client = make_client(ranked, fake)
    venues = find_venues(ranked, client, "fpl")
    assert venues[0].id == "S5"
    assert "Field" in dict(fake.calls[0].params)["search"]


def test_add_venue_papers_respects_year_and_frontier(conn, project):
    venue = ("S7", "Target Venue", "conference")
    fake = FakeOpenAlex(
        [
            work(1, "Old paper", 2015, doi="10.1/old", venue=venue),
            work(2, "New paper", 2024, doi="10.1/new", venue=venue),
            work(3, "Elsewhere", 2024, doi="10.1/other", venue=("S8", "Other", "journal")),
        ]
    )
    client = make_client(conn, fake)
    assert add_venue_papers(conn, client, project, "S7", 2020) == 1
    row = conn.execute("SELECT state FROM membership").fetchone()
    assert row["state"] == "frontier"


def test_target_venues_override_core_and_rank_journals(ranked, project):
    fake = FakeOpenAlex(
        [
            work(1, "Conference paper", doi="10.1000/a",
                 venue=("S1", "2024 International Conference on Field-Programmable "
                              "Logic and Applications (FPL)", "conference")),
            work(2, "Journal paper", doi="10.1000/b",
                 venue=("S2", "Physical Review Applied", "journal")),
        ]
    )
    client = make_client(ranked, fake)
    add_seeds(ranked, client, project, ["10.1000/a", "10.1000/b"])
    ids = [resolve(ranked, "doi", doi) for doi in ("10.1000/a", "10.1000/b")]
    assert ranks_for_works(ranked, ids)[ids[0]] == VenueRank("A", "ICORE2026")
    assert ids[1] not in ranks_for_works(ranked, ids)

    add_target(ranked, "Field Programmable Logic and Applications", "FPL", "A*")
    add_target(ranked, "Physical Review Applied", None, "B")
    ranks = ranks_for_works(ranked, ids)
    assert ranks[ids[0]] == VenueRank("A*", TARGET_EDITION)
    assert ranks[ids[1]] == VenueRank("B", TARGET_EDITION)
    assert unmatched_venues(ranked, project) == []

    set_targets(ranked, [])
    assert ranks_for_works(ranked, ids)[ids[0]].edition == "ICORE2026"


def test_target_list_file(tmp_path):
    path = tmp_path / "targets.yaml"
    path.write_text("venues:\n  - name: Nature Communications\n    rank: A*\n  - name: X\n    acronym: XC\n    rank: B\n")
    assert load_targets(path) == [
        {"title": "Nature Communications", "acronym": None, "rank": "A*"},
        {"title": "X", "acronym": "XC", "rank": "B"},
    ]
    path.write_text("venues:\n  - name: Somewhere\n    rank: excellent\n")
    with pytest.raises(BadInput):
        load_targets(path)


def test_core_list_download_saves_the_export(conn, tmp_path):
    import httpx

    seen = []

    def portal(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=NO_HEADER, headers={"content-type": "text/csv"})

    http = httpx.Client(transport=httpx.MockTransport(portal))
    path = download_core_csv("ICORE2026", tmp_path / "data", http)
    assert path.name == "icore2026.csv" and "source=ICORE2026" in seen[0]
    assert import_core_csv(conn, path) == 5

    refused = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<html>")))
    with pytest.raises(UpstreamError):
        download_core_csv("ICORE2026", tmp_path, refused)
    with pytest.raises(BadInput):
        download_core_csv("best list", tmp_path, http)
