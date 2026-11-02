import pytest
import numpy as np
from scipy.sparse import csr_matrix, lil_matrix

from ariadnesearch.expand import add_seeds, expand_project, record_decision
from ariadnesearch.identity import resolve
from ariadnesearch.rank import (
    GraphSnapshot,
    ProjectState,
    Scored,
    Weights,
    cocitation_scores,
    coupling_scores,
    evidence_for,
    explain,
    load_snapshot,
    load_weights,
    personalized_pagerank,
    rank,
    rank_by_citations,
    select_frontier,
)

BC_ONLY = Weights(bc=1, cc=0, ppr=0, emb=0, imp=0, neg=1)


def make_graph(references: dict[str, list[str]]) -> GraphSnapshot:
    """Build a snapshot from {paper: papers it cites}."""
    ids = sorted(set(references) | {p for refs in references.values() for p in refs})
    index = {paper: i for i, paper in enumerate(ids)}
    A = lil_matrix((len(ids), len(ids)))
    for src, refs in references.items():
        for dst in refs:
            A[index[src], index[dst]] = 1
    return GraphSnapshot(
        ids=ids,
        A=A.tocsr(),
        n_refs={paper: len(references.get(paper, [])) for paper in ids},
        year={paper: 2024 for paper in ids},
        percentile={paper: 0.5 for paper in ids},
    )


def test_long_reference_lists_do_not_dominate_coupling():
    extra = [f"x{i}" for i in range(98)]
    g = make_graph(
        {
            "seed": ["r1", "r2"],
            "focused": ["r1", "r2"],
            "survey": ["r1", "r2", *extra],
        }
    )
    scores = coupling_scores(g, ["focused", "survey"], ["seed"])
    assert scores["focused"] > scores["survey"]


def test_unknown_references_omit_the_coupling_term():
    g = make_graph({"seed": ["r1"], "citing": ["seed", "unknown"], "unknown": []})
    state = ProjectState(accepted={"seed"}, rejected=set(), frontier={"unknown"})
    assert "bc" not in rank(g, state, Weights())[0].terms


def test_similarity_to_rejected_papers_lowers_the_score():
    g = make_graph(
        {
            "seed": ["r1", "r2"],
            "rejected": ["r3", "r4"],
            "near_rejected": ["r1", "r3"],
            "clean": ["r1", "r5"],
        }
    )
    state = ProjectState(
        accepted={"seed"},
        rejected={"rejected"},
        frontier={"near_rejected", "clean"},
    )
    results = rank(g, state, BC_ONLY)
    assert [item.work_id for item in results] == ["clean", "near_rejected"]


def test_results_are_sorted_and_explain_shared_references():
    g = make_graph(
        {"seed": ["r1", "r2"], "strong": ["r1", "r2"], "weak": ["r1", "r9"]}
    )
    state = ProjectState(
        accepted={"seed"}, rejected=set(), frontier={"strong", "weak"}
    )
    results = rank(g, state, BC_ONLY)
    assert results[0].work_id == "strong"
    assert results[0].score >= results[1].score
    assert set(explain(g, state, ["strong"])["strong"]["shared_refs"]) == {"r1", "r2"}


def test_core_rank_shifts_scores_and_unknown_venues_are_neutral():
    g = make_graph({"seed": ["r1"], "top": ["r1"], "low": ["r1"], "journal": ["r1"]})
    g.venue_rank.update({"top": "A*", "low": "C"})
    state = ProjectState({"seed"}, set(), {"top", "low", "journal"})
    scores = {r.work_id: r.score for r in rank(g, state, Weights(venue=0.2))}
    assert scores["top"] == pytest.approx(scores["journal"] + 0.1)
    assert scores["low"] == pytest.approx(scores["journal"] - 0.05)
    terms = {r.work_id: r.terms for r in rank(g, state, Weights())}
    assert "venue" not in terms["journal"]

    flat = {r.work_id: r.score for r in rank(g, state, Weights(venue=0))}
    assert flat["top"] == flat["low"] == flat["journal"]


def test_cocitation_counts_shared_citers():
    g = make_graph(
        {"a": ["seed", "partner"], "b": ["seed", "partner"], "c": ["loner"]}
    )
    scores = cocitation_scores(g, ["partner", "loner"], ["seed"])
    assert scores["partner"] == pytest.approx(1.0)
    assert scores["loner"] == 0


def test_cocitation_uses_global_counts_for_widely_cited_papers():
    g = make_graph({"a": ["seed", "famous", "niche"]})
    g.cited_by = {"famous": 10000, "niche": 1}
    scores = cocitation_scores(g, ["famous", "niche"], ["seed"])
    assert scores["niche"] > scores["famous"]


def test_pagerank_favours_papers_near_the_seeds():
    g = make_graph({"seed": ["near"], "near": ["far"], "far": ["farther"]})
    scores = personalized_pagerank(g, ["seed"])
    assert scores["near"] > scores["far"] > scores["farther"]
    assert sum(scores.values()) == pytest.approx(1.0)


def test_pagerank_handles_no_seeds_and_isolated_papers():
    g = make_graph({"a": [], "b": ["c"]})
    assert set(personalized_pagerank(g, []).values()) == {0.0}
    assert personalized_pagerank(g, ["a"])["a"] == pytest.approx(1.0)


def test_recent_papers_are_not_punished_for_few_citations():
    g = make_graph(
        {
            "seed": ["r1", "r2"],
            "old": ["r1"],
            "new": ["r1"],
            "c1": ["seed", "old"],
            "c2": ["seed", "old"],
        }
    )
    g.year.update({"seed": 2020, "old": 2020, "new": 2026, "c1": 2026})
    g.percentile.update({"old": 0.9, "new": 0.1})
    state = ProjectState({"seed"}, set(), {"old", "new"})

    damped = {item.work_id: item for item in rank(g, state, Weights(recent_months=18))}
    undamped = {item.work_id: item for item in rank(g, state, Weights(recent_months=0))}
    assert damped["new"].score > undamped["new"].score
    assert damped["old"].terms == undamped["old"].terms


def test_missing_percentile_is_left_out_rather_than_zero():
    g = make_graph({"seed": ["r1"], "a": ["r1"]})
    g.percentile["a"] = None
    item = rank(g, ProjectState({"seed"}, set(), {"a"}), Weights())[0]
    assert "imp" not in item.terms


def test_empty_frontier_returns_nothing():
    g = make_graph({"seed": []})
    assert rank(g, ProjectState({"seed"}, set(), set()), Weights()) == []


def test_no_known_papers_still_ranks_without_failing():
    g = make_graph({"a": ["b"], "b": []})
    results = rank(g, ProjectState(set(), set(), {"a", "b"}), Weights())
    assert len(results) == 2


def test_evidence_lists_direct_links():
    g = make_graph({"seed": ["r1"], "cand": ["seed", "r1"], "citer": ["cand", "seed"]})
    evidence = evidence_for(g, ProjectState({"seed"}, set(), {"cand"}), "cand")
    assert evidence["cites_known"] == ["seed"]
    assert evidence["shared_refs"] == ["r1"]
    assert evidence["shared_citers"] == ["citer"]
    assert evidence["cited_by_known"] == []


def test_select_frontier_keeps_top_results_and_marks_exploration():
    results = [Scored(f"p{i}", 1 - i / 100) for i in range(60)]
    chosen = select_frontier(results, 10, 0.2, seed=1)
    assert [item.work_id for item in chosen[:8]] == [f"p{i}" for i in range(8)]
    assert len(chosen) == 10
    assert all(item.explore for item in chosen[8:])
    assert not any(item.explore for item in chosen[:8])


def test_select_frontier_is_repeatable_and_handles_short_lists():
    make = lambda: [Scored(f"p{i}", 1 - i / 100) for i in range(60)]  # noqa: E731
    first = select_frontier(make(), 10, 0.2, seed=3)
    second = select_frontier(make(), 10, 0.2, seed=3)
    assert [i.work_id for i in first] == [i.work_id for i in second]
    assert len(select_frontier([Scored("a", 1.0)], 10, 0.2)) == 1


def test_citation_baseline_sorts_by_global_count():
    g = make_graph({"a": [], "b": []})
    g.cited_by = {"a": 5, "b": 50}
    assert rank_by_citations(g, ProjectState(set(), set(), {"a", "b"})) == ["b", "a"]


def expanded(conn, client, project):
    add_seeds(conn, client, project, ["10.1109/seed.1", "10.1145/seed.2"])
    expand_project(conn, client, project)


def test_snapshot_matches_stored_graph(conn, client, project):
    expanded(conn, client, project)
    g, state = load_snapshot(conn, project)
    assert isinstance(g.A, csr_matrix)
    assert g.A.shape == (len(g.ids), len(g.ids))
    assert len(state.accepted) == 2
    assert resolve(conn, "openalex", "W30") in state.frontier
    assert g.A.nnz == conn.execute("SELECT COUNT(*) FROM cites").fetchone()[0]


def test_empty_project_snapshot(conn, project):
    g, state = load_snapshot(conn, project)
    assert g.ids == []
    assert rank(g, state, Weights()) == []


def test_snapshot_can_hide_papers_that_leak_answers(conn, client, project):
    expanded(conn, client, project)
    hidden = resolve(conn, "openalex", "W20")
    g, state = load_snapshot(conn, project, exclude=[hidden])
    assert hidden not in state.frontier
    position = g.ids.index(hidden) if hidden in g.ids else None
    if position is not None:
        assert g.A[position].nnz == 0

    g, state = load_snapshot(conn, project, max_year=2023)
    assert resolve(conn, "openalex", "W22") not in state.frontier


def test_read_papers_count_as_known(conn, client, project):
    expanded(conn, client, project)
    work_id = resolve(conn, "openalex", "W30")
    record_decision(conn, project, work_id, "read")
    _, state = load_snapshot(conn, project)
    assert work_id in state.accepted
    assert work_id not in state.frontier


def test_end_to_end_order_is_sensible(conn, client, project):
    expanded(conn, client, project)
    g, state = load_snapshot(conn, project)
    order = [item.work_id for item in rank(g, state, load_weights(conn, project))]
    place = {n: order.index(resolve(conn, "openalex", f"W{n}")) for n in (10, 11, 12, 13, 20, 21, 22)}
    # Cited by both seeds beats cited by one.
    assert max(place[10], place[11]) < min(place[12], place[13])
    # Citing both seeds beats citing one.
    assert max(place[20], place[21]) < place[22]


def test_direct_citation_counts_towards_coupling():
    g = make_graph({"seed": ["r1", "r2"], "citer": ["seed", "other"], "stranger": ["other"]})
    scores = coupling_scores(g, ["citer", "stranger"], ["seed"])
    assert scores["citer"] > 0
    assert scores["stranger"] == 0


def test_saved_weights_are_used(conn, project):
    conn.execute(
        "UPDATE project SET weights_json = ? WHERE id = ?",
        ('{"neg": 3.0, "unknown": 1}', project),
    )
    assert load_weights(conn, project).neg == 3.0


def test_scores_are_finite(conn, client, project):
    expanded(conn, client, project)
    g, state = load_snapshot(conn, project)
    scores = [item.score for item in rank(g, state, Weights())]
    assert np.isfinite(scores).all()
