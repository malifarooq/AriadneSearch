"""Test the ranker against a citation-count baseline.

Usage:
    python eval/loo.py holdout refs.bib --fraction 0.2 --exclude 10.1109/survey.1
    python eval/loo.py temporal

holdout:  hide part of a curated bibliography, seed with the rest, then measure
          recall@50. Use --exclude for any paper that cites the whole bibliography,
          such as the survey it came from, because its references leak the answer.
temporal: seed with citations from your older papers, then measure how many of
          your newest paper's citations the tool predicts.

Both report "reachable", the share of targets that entered the frontier at all.
Recall cannot exceed it.

A curated bibliography reflects your own past choices, so also judge some
suggestions by hand.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from ariadnesearch.config import load_config
from ariadnesearch.db import create_project, delete_project, init_db, utc_now
from ariadnesearch.errors import BadInput, AriadneError
from ariadnesearch.expand import add_work_ids_as_seeds, expand_project
from ariadnesearch.identity import resolve
from ariadnesearch.mine import parse_bib, resolve_entries
from ariadnesearch.openalex import OpenAlexClient
from ariadnesearch.rank import load_snapshot, load_weights, rank, rank_by_citations
from ariadnesearch.records import normalize_doi
from ariadnesearch.tune import holdout_split


@dataclass
class EvalResult:
    method: str
    k: int
    recall: float
    n_targets: int


def recall_at_k(ranked: list[str], targets: set[str], k: int = 50) -> float:
    """Return the share of targets found in the top k."""
    if not targets:
        return 0.0
    return len(set(ranked[:k]) & targets) / len(targets)


def _evaluate(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    seeds: set[str],
    targets: set[str],
    k: int,
    top_k: int,
    exclude: set[str],
    max_year: int | None = None,
) -> list[EvalResult]:
    project_id = create_project(conn, f"eval-{utc_now()}")
    try:
        add_work_ids_as_seeds(conn, project_id, seeds)
        expand_project(conn, client, project_id, top_k, per_paper=None)
        snapshot, state = load_snapshot(conn, project_id, exclude, max_year)
        ranked = [
            item.work_id
            for item in rank(snapshot, state, load_weights(conn, project_id))
        ]
        baseline = rank_by_citations(snapshot, state)
    finally:
        delete_project(conn, project_id)
    n = len(targets)
    reachable = len(targets & state.frontier) / n if n else 0.0
    return [
        EvalResult("ranker", k, recall_at_k(ranked, targets, k), n),
        EvalResult("citation count", k, recall_at_k(baseline, targets, k), n),
        EvalResult("reachable", len(ranked), reachable, n),
    ]


def run_holdout(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    bib_path: Path,
    fraction: float,
    k: int = 50,
    top_k: int = 20,
    exclude_dois: tuple[str, ...] = (),
    seed: int = 0,
) -> list[EvalResult]:
    """Compare `rank.rank` with sorting by citation count."""
    entries = list(parse_bib(bib_path).values())
    found, _ = resolve_entries(conn, client, entries)
    excluded = {
        work_id
        for doi in exclude_dois
        if (work_id := resolve(conn, "doi", normalize_doi(doi)))
    }
    papers = sorted(set(found.values()) - excluded)
    seeds, targets = holdout_split(papers, fraction, seed)
    return _evaluate(conn, client, set(seeds), set(targets), k, top_k, excluded)


def run_temporal(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    k: int = 50,
    top_k: int = 20,
) -> list[EvalResult]:
    """Predict your newest paper's citations from your earlier ones."""
    papers = conn.execute(
        "SELECT id, doi, year FROM my_paper WHERE status = 'published' "
        "AND year IS NOT NULL ORDER BY year DESC, id DESC"
    ).fetchall()
    if len(papers) < 2:
        raise BadInput("Run `ariadne mine sync` with at least two published papers.")
    newest, older = papers[0], papers[1:]

    def cited_by(paper_ids: list[int]) -> set[str]:
        marks = ",".join("?" * len(paper_ids))
        rows = conn.execute(
            f"SELECT DISTINCT work_id FROM my_citation WHERE my_paper_id IN ({marks})",
            paper_ids,
        )
        return {row["work_id"] for row in rows}

    seeds = cited_by([paper["id"] for paper in older])
    targets = cited_by([newest["id"]]) - seeds
    excluded = {resolve(conn, "doi", newest["doi"])} - {None}
    return _evaluate(
        conn, client, seeds, targets, k, top_k, excluded, max_year=newest["year"]
    )


def _print_results(results: list[EvalResult]) -> None:
    print(f"Targets: {results[0].n_targets}")
    for item in results:
        print(f"  {item.method:<15} {item.recall:6.1%}  (k={item.k})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-k", type=int, default=50)
    parser.add_argument("--top", type=int, default=20, help="frontier papers to expand")
    commands = parser.add_subparsers(dest="command", required=True)
    holdout = commands.add_parser("holdout")
    holdout.add_argument("bib", type=Path)
    holdout.add_argument("--fraction", type=float, default=0.2)
    holdout.add_argument("--seed", type=int, default=0)
    holdout.add_argument("--exclude", action="append", default=[], metavar="DOI")
    commands.add_parser("temporal")
    args = parser.parse_args()

    config = load_config()
    conn = init_db(config.graph_db)
    client = OpenAlexClient(
        conn, config.openalex_api_key, daily_usd=config.openalex_daily_usd
    )
    try:
        if args.command == "holdout":
            results = run_holdout(
                conn,
                client,
                args.bib,
                args.fraction,
                args.k,
                args.top,
                tuple(args.exclude),
                args.seed,
            )
        else:
            results = run_temporal(conn, client, args.k, args.top)
    except AriadneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
