"""Ranking weights fitted to the papers a project starts from.

Hide a fifth of the seed papers, grow a graph from the rest, and score every
weight setting in GRID by how many hidden seeds it ranks in the top `k`. Each
split is expanded once, and the settings only recombine the stored score terms,
so trying them costs no API calls. The best setting is saved on the project.
"""

from __future__ import annotations

import itertools
import json
import random
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from ariadnesearch.db import (
    create_project,
    delete_project,
    is_author_project,
    log_event,
    utc_now,
)
from ariadnesearch.errors import BadInput
from ariadnesearch.expand import add_work_ids_as_seeds, expand_project
from ariadnesearch.openalex import OpenAlexClient
from ariadnesearch.prune import prune
from ariadnesearch.rank import (
    KNOWN_STATES,
    RECENT_DAMPING,
    Weights,
    _is_recent,
    load_snapshot,
    rank,
    rank_by_citations,
)

GRID = {
    "bc": [0.0, 0.5, 1.0, 2.0],
    "cc": [0.0, 0.5, 1.0, 2.0],
    "ppr": [0.0, 0.5, 1.0, 2.0],
    "imp": [0.0, 0.25, 0.5, 1.0],
    "venue": [0.0, 0.1, 0.2, 0.4],
}
HOLDOUT_FRACTION = 0.2
MIN_SEEDS = 15  # fewer gives about three hidden papers per split, too few to judge


@dataclass
class SweepResult:
    configs: list[dict]
    recall: np.ndarray  # configs x splits
    default_recall: list[float] = field(default_factory=list)
    citation_recall: list[float] = field(default_factory=list)
    reachable: list[float] = field(default_factory=list)
    targets: list[int] = field(default_factory=list)

    def best(self) -> tuple[dict, float]:
        """Return the setting with the best mean recall, ties broken by its worst split."""
        means, worst = self.recall.mean(axis=1), self.recall.min(axis=1)
        index = int(np.lexsort((-worst, -means))[0])
        return self.configs[index], float(means[index])


def holdout_split(
    work_ids: list[str], fraction: float, seed: int = 0
) -> tuple[list[str], list[str]]:
    """Split papers into (seeds, hidden targets)."""
    if not 0 < fraction < 1:
        raise BadInput("--fraction must be between 0 and 1.")
    shuffled = sorted(work_ids)
    random.Random(seed).shuffle(shuffled)
    n_hidden = max(1, round(len(shuffled) * fraction))
    return sorted(shuffled[n_hidden:]), sorted(shuffled[:n_hidden])


def _grid() -> list[dict]:
    return [
        dict(zip(GRID, values))
        for values in itertools.product(*GRID.values())
        if not values[0] == values[1] == values[2] == 0
    ]


def _recall(order: np.ndarray, is_target: np.ndarray, k: int) -> float:
    return float(is_target[order[:k]].sum() / max(is_target.sum(), 1))


def sweep(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    papers: list[str],
    splits: int = 3,
    k: int = 50,
    top_k: int = 20,
    exclude: set[str] | None = None,
    progress=None,
) -> SweepResult:
    """Score every weight setting on `splits` holdout splits of `papers`."""
    configs = _grid()
    result = SweepResult(configs, np.zeros((len(configs), splits)))
    default = Weights()
    for column in range(splits):
        seeds, hidden = holdout_split(papers, HOLDOUT_FRACTION, column)
        targets = set(hidden)
        project_id = create_project(conn, f"tune-{column}-{utc_now()}")
        try:
            add_work_ids_as_seeds(conn, project_id, set(seeds))
            expand_project(conn, client, project_id, top_k, per_paper=None)
            graph, state = load_snapshot(conn, project_id, exclude or set(), None)
        finally:
            delete_project(conn, project_id)

        results = rank(graph, state, default)
        ids = [item.work_id for item in results]
        is_target = np.array([work_id in targets for work_id in ids], dtype=bool)

        def column_of(key: str, missing: float) -> np.ndarray:
            return np.array([item.terms.get(key, missing) for item in results], dtype=float)

        bc, imp = column_of("bc", np.nan), column_of("imp", np.nan)
        has_bc, has_imp = ~np.isnan(bc), ~np.isnan(imp)
        bc, imp = np.nan_to_num(bc), np.nan_to_num(imp)
        cc, ppr = column_of("cc", 0.0), column_of("ppr", 0.0)
        neg, venue = column_of("neg", 0.0), column_of("venue", 0.0)
        years = [year for year in graph.year.values() if year is not None]
        latest = max(years) if years else None
        damp = np.array(
            [
                RECENT_DAMPING if _is_recent(graph.year.get(work_id), latest, default.recent_months) else 1.0
                for work_id in ids
            ]
        )
        for row, setting in enumerate(configs):
            # rank.py's formula, applied to every setting at once.
            total = setting["bc"] * has_bc + setting["cc"] * damp + setting["ppr"] + setting["imp"] * damp * has_imp
            positive = (
                setting["bc"] * bc + setting["cc"] * damp * cc + setting["ppr"] * ppr
                + setting["imp"] * damp * imp
            )
            score = np.divide(positive, total, out=np.zeros_like(total), where=total > 0)
            score = score - default.neg * neg + setting["venue"] * venue
            result.recall[row, column] = _recall(np.lexsort((np.array(ids), -score)), is_target, k)

        result.default_recall.append(_recall(np.arange(len(ids)), is_target, k))
        baseline = rank_by_citations(graph, state)
        result.citation_recall.append(len(set(baseline[:k]) & targets) / len(targets))
        result.reachable.append(len(targets & state.frontier) / len(targets))
        result.targets.append(len(targets))
        if progress:
            progress(column, result)
    return result


def tune_project(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    project_id: int,
    splits: int = 3,
    k: int = 50,
    progress=None,
) -> tuple[dict, SweepResult]:
    """Fit a project's weights to its seed papers and save them. Return (weights, sweep)."""
    marks = ",".join("?" * len(KNOWN_STATES))
    papers = [
        row[0]
        for row in conn.execute(
            f"SELECT work_id FROM membership WHERE project_id = ? AND state IN ({marks})",
            (project_id, *KNOWN_STATES),
        )
    ]
    if is_author_project(conn, project_id):
        raise BadInput(
            "Fitting weights needs a topic project. An author project's seeds are "
            "one person's papers, not a reading list."
        )
    if len(papers) < MIN_SEEDS:
        raise BadInput(
            f"Tuning needs at least {MIN_SEEDS} seed or accepted papers, and this project "
            f"has {len(papers)}. It keeps the default weights."
        )
    result = sweep(conn, client, papers, splits, k, progress=progress)
    prune(conn)  # the splits' graphs are no longer needed
    best, recall = result.best()
    row = conn.execute("SELECT weights_json FROM project WHERE id = ?", (project_id,)).fetchone()
    weights = {**json.loads(row["weights_json"] or "{}"), **best}
    conn.execute(
        "UPDATE project SET weights_json = ? WHERE id = ?", (json.dumps(weights), project_id)
    )
    log_event(
        conn,
        project_id,
        "tune",
        {
            "weights": best,
            "recall": recall,
            "default_recall": float(np.mean(result.default_recall)),
            "papers": len(papers),
        },
    )
    conn.commit()
    return best, result
