"""Score frontier papers. `rank` is a pure function with no network use.

score = (w.bc * BC + w.cc * CC + w.ppr * PPR + w.imp * IMP) / (sum of weights used)
        - w.neg * NEG + w.venue * VENUE

VENUE is the paper's CORE rank: A* +0.5, A +0.25, B 0, C -0.25. Venues CORE does
not rank, such as most journals, get 0, so a missing rank never counts against a paper.

The UI and the evaluation scripts share this code. The default weights are
neutral. `ariadne tune` fits a project's weights to its seed papers.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from functools import cached_property

import numpy as np
from scipy.sparse import csr_matrix, diags, identity

KNOWN_STATES = ("seed", "accepted", "read")
RECENT_DAMPING = 0.25  # new papers have few incoming citations
EXPLORE_POOL_FACTOR = 5
VENUE_BONUS = {"A*": 0.5, "A": 0.25, "B": 0.0, "C": -0.25}


@dataclass(frozen=True)
class Weights:
    # Neutral starting points. `ariadne tune` replaces them for a project with
    # weights fitted to the papers it was started from.
    bc: float = 1.0
    cc: float = 1.0
    ppr: float = 1.0
    emb: float = 0.0  # off until embeddings are added
    imp: float = 0.5
    neg: float = 1.0
    venue: float = 0.2
    recent_months: int = 18  # shift weight to BC for newer papers
    explore_fraction: float = 0.10  # share of results kept for exploration


@dataclass
class GraphSnapshot:
    """Graph data for one project. A[i, j] = 1 when ids[i] cites ids[j]."""

    ids: list[str]
    A: csr_matrix
    n_refs: dict[str, int | None]
    year: dict[str, int | None]
    percentile: dict[str, float | None]
    cited_by: dict[str, int | None] = field(default_factory=dict)
    venue_rank: dict[str, str] = field(default_factory=dict)  # CORE rank, if any

    # Built once per snapshot, since scoring and evidence reuse them.
    @cached_property
    def index(self) -> dict[str, int]:
        return {work_id: i for i, work_id in enumerate(self.ids)}

    @cached_property
    def cited(self) -> csr_matrix:
        """Row i lists the papers citing ids[i]."""
        return self.A.T.tocsr()

    @cached_property
    def links_with_self(self) -> csr_matrix:
        return (self.A + identity(len(self.ids), format="csr")).tocsr()


@dataclass
class ProjectState:
    accepted: set[str]  # seeds, accepted and read papers
    rejected: set[str]
    frontier: set[str]


@dataclass
class Scored:
    work_id: str
    score: float
    terms: dict[str, float] = field(default_factory=dict)  # per-term scores
    evidence: dict[str, list[str]] = field(default_factory=dict)  # "why this paper"
    explore: bool = False


def load_weights(conn: sqlite3.Connection, project_id: int) -> Weights:
    """Return the project's weights, using defaults for anything unset."""
    row = conn.execute(
        "SELECT weights_json FROM project WHERE id = ?", (project_id,)
    ).fetchone()
    saved = json.loads(row["weights_json"]) if row else {}
    allowed = asdict(Weights())
    return Weights(**{**allowed, **{k: v for k, v in saved.items() if k in allowed}})


def load_snapshot(
    conn: sqlite3.Connection,
    project_id: int,
    exclude: Iterable[str] = (),
    max_year: int | None = None,
    candidates: Iterable[str] = (),
) -> tuple[GraphSnapshot, ProjectState]:
    """Build in-memory inputs for `rank`.

    `exclude` and `max_year` let evaluations hide papers that would leak answers.
    `candidates` are papers outside the project to score as if they were in the
    frontier, so expansion can choose which of them to add.
    """
    members = conn.execute(
        """
        SELECT m.work_id, m.state, w.year, w.n_refs, w.oa_pct, w.oa_cited_by
        FROM membership m JOIN work w ON w.id = m.work_id
        WHERE m.project_id = ?
        """,
        (project_id,),
    ).fetchall()
    extra = sorted(set(candidates) - {row["work_id"] for row in members})
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS snapshot_extra (work_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM temp.snapshot_extra")
    conn.executemany(
        "INSERT INTO temp.snapshot_extra(work_id) VALUES (?)", [(item,) for item in extra]
    )
    members += conn.execute(
        """
        SELECT w.id AS work_id, 'frontier' AS state, w.year, w.n_refs, w.oa_pct,
               w.oa_cited_by
        FROM temp.snapshot_extra x JOIN work w ON w.id = x.work_id
        """
    ).fetchall()
    hidden = set(exclude)
    if max_year is not None:
        hidden |= {
            row["work_id"]
            for row in members
            if row["year"] is not None and row["year"] > max_year
        }

    # Plain tuples, and UNION ALL with the overlap excluded, since a large project
    # has over a million edges and sqlite3.Row and UNION's de-duplication are slow.
    cursor = conn.cursor()
    cursor.row_factory = None
    edges = cursor.execute(
        """
        WITH members AS (
            SELECT work_id FROM membership WHERE project_id = ?
            UNION SELECT work_id FROM temp.snapshot_extra
        )
        SELECT src, dst FROM cites WHERE src IN members
        UNION ALL
        SELECT src, dst FROM cites
        WHERE dst IN members AND src NOT IN members
        """,
        (project_id,),
    ).fetchall()
    if hidden:
        edges = [edge for edge in edges if edge[0] not in hidden]
    sources = [edge[0] for edge in edges]
    targets = [edge[1] for edge in edges]

    ids = sorted(
        ({row["work_id"] for row in members} - hidden) | set(sources) | set(targets)
    )
    index = {work_id: i for i, work_id in enumerate(ids)}
    rows = np.fromiter(map(index.__getitem__, sources), dtype=np.int64, count=len(edges))
    cols = np.fromiter(map(index.__getitem__, targets), dtype=np.int64, count=len(edges))
    matrix = csr_matrix(
        (np.ones(len(edges)), (rows, cols)), shape=(len(ids), len(ids))
    )

    visible = [row for row in members if row["work_id"] not in hidden]
    # Imported here because venues imports expand, which imports this module.
    from ariadnesearch.venues import ranks_for_works

    frontier = [row["work_id"] for row in visible if row["state"] == "frontier"]
    snapshot = GraphSnapshot(
        ids=ids,
        A=matrix,
        n_refs={row["work_id"]: row["n_refs"] for row in visible},
        year={row["work_id"]: row["year"] for row in visible},
        percentile={row["work_id"]: row["oa_pct"] for row in visible},
        cited_by={row["work_id"]: row["oa_cited_by"] for row in visible},
        venue_rank={
            work_id: value.rank
            for work_id, value in ranks_for_works(conn, frontier).items()
        },
    )
    state = ProjectState(
        accepted={r["work_id"] for r in visible if r["state"] in KNOWN_STATES},
        rejected={r["work_id"] for r in visible if r["state"] == "rejected"},
        frontier={r["work_id"] for r in visible if r["state"] == "frontier"},
    )
    return snapshot, state


def _inverse(values: np.ndarray) -> np.ndarray:
    return np.divide(1.0, values, out=np.zeros_like(values, dtype=float), where=values > 0)


def _mean_cosine(
    candidates: csr_matrix,
    references: csr_matrix,
    candidate_sizes: np.ndarray | None = None,
) -> np.ndarray:
    """Mean cosine similarity of each candidate row with the reference rows."""
    if references.shape[0] == 0:
        return np.zeros(candidates.shape[0])
    if candidate_sizes is None:
        candidate_sizes = candidates.getnnz(axis=1).astype(float)
    reference_sizes = references.getnnz(axis=1).astype(float)
    overlap = candidates @ references.T
    scaled = (
        diags(_inverse(np.sqrt(candidate_sizes)))
        @ overlap
        @ diags(_inverse(np.sqrt(reference_sizes)))
    )
    return np.asarray(scaled.sum(axis=1)).ravel() / references.shape[0]


def _positions(g: GraphSnapshot, work_ids: Iterable[str]) -> list[int]:
    return [g.index[work_id] for work_id in work_ids if work_id in g.index]


def coupling_scores(
    g: GraphSnapshot, candidates: list[str], reference_set: list[str]
) -> dict[str, float]:
    """Bibliographic coupling, from shared references.

    Each paper's list includes the paper itself, so a direct citation counts as
    overlap. Without this, a paper citing a seed could score zero. Scores are
    cosine-normalized so long lists do not dominate, and averaged over
    `reference_set`.
    """
    links = g.links_with_self
    positions = _positions(g, candidates)
    scores = _mean_cosine(links[positions], links[_positions(g, reference_set)])
    return {g.ids[p]: float(s) for p, s in zip(positions, scores)}


def cocitation_scores(
    g: GraphSnapshot, candidates: list[str], reference_set: list[str]
) -> dict[str, float]:
    """Co-citation, from shared citing papers.

    A candidate's global citation count is used when it exceeds what is stored,
    so widely cited papers are not favoured by a partial citer list.
    """
    cited = g.cited
    positions = _positions(g, candidates)
    local = cited[positions]
    sizes = np.array(
        [
            max(count, g.cited_by.get(g.ids[p]) or 0)
            for p, count in zip(positions, local.getnnz(axis=1))
        ],
        dtype=float,
    )
    scores = _mean_cosine(local, cited[_positions(g, reference_set)], sizes)
    return {g.ids[p]: float(s) for p, s in zip(positions, scores)}


def personalized_pagerank(
    g: GraphSnapshot, seeds: list[str], damping: float = 0.85, iterations: int = 50
) -> dict[str, float]:
    """Personalized PageRank from the seeds over undirected citation links."""
    n = len(g.ids)
    positions = _positions(g, seeds)
    if n == 0 or not positions:
        return {work_id: 0.0 for work_id in g.ids}
    links = ((g.A + g.A.T) > 0).astype(float).tocsr()
    degree = np.asarray(links.sum(axis=1)).ravel()
    step = (diags(_inverse(degree)) @ links).T.tocsr()
    restart = np.zeros(n)
    restart[positions] = 1.0 / len(positions)
    scores = restart.copy()
    for _ in range(iterations):
        dangling = scores[degree == 0].sum()
        scores = (
            damping * (step @ scores + dangling * restart) + (1 - damping) * restart
        )
    return {work_id: float(value) for work_id, value in zip(g.ids, scores)}


def _scaled(scores: dict[str, float]) -> dict[str, float]:
    top = max(scores.values(), default=0.0)
    return {key: value / top for key, value in scores.items()} if top > 0 else scores


def _is_recent(year: int | None, latest: int | None, months: int) -> bool:
    if months <= 0 or year is None or latest is None:
        return False
    return year >= latest - months / 12


def explain(
    g: GraphSnapshot, s: ProjectState, work_ids: Iterable[str]
) -> dict[str, dict[str, list[str]]]:
    """Explain how each paper connects to the known papers."""
    known = _positions(g, sorted(s.accepted))
    known_set = set(known)
    known_references = set(g.A[known].indices)
    known_citers = set(g.cited[known].indices)

    def names(positions: set[int]) -> list[str]:
        return [g.ids[i] for i in sorted(positions)]

    explained = {}
    for work_id in work_ids:
        position = g.index[work_id]
        references = set(g.A[position].indices)
        citers = set(g.cited[position].indices)
        explained[work_id] = {
            "shared_refs": names(references & known_references),
            "shared_citers": names(citers & known_citers),
            "cites_known": names(references & known_set),
            "cited_by_known": names(citers & known_set),
        }
    return explained


def evidence_for(
    g: GraphSnapshot, s: ProjectState, work_id: str
) -> dict[str, list[str]]:
    """Explain how one paper connects to the known papers."""
    return explain(g, s, [work_id])[work_id]


def rank(g: GraphSnapshot, s: ProjectState, w: Weights) -> list[Scored]:
    """Return frontier papers sorted by descending score.

    When a paper's references are unknown, "bc" is omitted from its `terms`.
    Terms without data are left out, and the remaining weights renormalized.
    Evidence is left empty here. Use `explain` for the papers you show.
    """
    present = set(g.ids)
    candidates = sorted(s.frontier & present)
    accepted = sorted(s.accepted & present)
    rejected = sorted(s.rejected & present)
    if not candidates:
        return []

    coupling = _scaled(coupling_scores(g, candidates, accepted))
    cocitation = _scaled(cocitation_scores(g, candidates, accepted))
    pagerank = personalized_pagerank(g, accepted)
    walk = _scaled({key: pagerank[key] for key in candidates})
    penalty = coupling_scores(g, candidates, rejected)
    years = [year for year in g.year.values() if year is not None]
    latest = max(years) if years else None

    results = []
    for work_id in candidates:
        recent = _is_recent(g.year.get(work_id), latest, w.recent_months)
        damping = RECENT_DAMPING if recent else 1.0
        terms: dict[str, float] = {}
        weighted: list[tuple[float, float]] = []
        if g.n_refs.get(work_id):
            terms["bc"] = coupling[work_id]
            weighted.append((w.bc, terms["bc"]))
        terms["cc"] = cocitation[work_id]
        weighted.append((w.cc * damping, terms["cc"]))
        terms["ppr"] = float(np.sqrt(walk[work_id]))  # reduces the skew
        weighted.append((w.ppr, terms["ppr"]))
        percentile = g.percentile.get(work_id)
        if percentile is not None:
            terms["imp"] = float(percentile)
            weighted.append((w.imp * damping, terms["imp"]))

        total = sum(weight for weight, _ in weighted)
        positive = sum(weight * value for weight, value in weighted)
        positive = positive / total if total > 0 else 0.0
        terms["neg"] = penalty[work_id] if g.n_refs.get(work_id) else 0.0
        score = positive - w.neg * terms["neg"]
        bonus = VENUE_BONUS.get(g.venue_rank.get(work_id, ""))
        if bonus is not None:
            terms["venue"] = bonus
            score += w.venue * bonus
        results.append(Scored(work_id, score, terms))

    results.sort(key=lambda item: (-item.score, item.work_id))
    return results


def rank_by_citations(g: GraphSnapshot, s: ProjectState) -> list[str]:
    """Baseline for evaluation: frontier papers by global citation count."""
    return sorted(
        s.frontier, key=lambda work_id: (-(g.cited_by.get(work_id) or 0), work_id)
    )


def select_frontier(
    results: list[Scored], k: int, explore_fraction: float, seed: int = 0
) -> list[Scored]:
    """Take the top results, plus a score-weighted sample from further down."""
    n_explore = int(round(k * explore_fraction))
    n_top = k - n_explore
    chosen = results[:n_top]
    pool = results[n_top : n_top + n_explore * EXPLORE_POOL_FACTOR]
    rng = random.Random(seed)
    keyed = sorted(
        pool,
        key=lambda item: rng.random() ** (1 / max(item.score, 1e-6)),
        reverse=True,
    )
    for item in keyed[:n_explore]:
        item.explore = True
        chosen.append(item)
    return chosen
