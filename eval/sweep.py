"""Tune ranking weights on a curated bibliography and print the full comparison.

Usage:
    python eval/sweep.py refs.bib --exclude DOI_OF_THE_PAPER_THE_BIB_CAME_FROM

`ariadne tune PROJECT` does the same with a project's seed papers and saves the
winner. This script prints more: the citation-count baseline, the share of hidden
papers reachable at all, and the best settings for each venue weight.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from ariadnesearch.config import load_config
from ariadnesearch.db import init_db
from ariadnesearch.identity import resolve
from ariadnesearch.mine import parse_bib, resolve_entries
from ariadnesearch.openalex import OpenAlexClient
from ariadnesearch.records import normalize_doi
from ariadnesearch.tune import GRID, sweep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bib", type=Path)
    parser.add_argument("--exclude", action="append", default=[], metavar="DOI")
    parser.add_argument("--splits", type=int, default=3)
    parser.add_argument("-k", type=int, default=50)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    config = load_config()
    conn = init_db(config.graph_db)
    client = OpenAlexClient(
        conn, config.openalex_api_key, daily_usd=config.openalex_daily_usd
    )
    found, _ = resolve_entries(conn, client, list(parse_bib(args.bib).values()))
    excluded = {
        work_id
        for doi in args.exclude
        if (work_id := resolve(conn, "doi", normalize_doi(doi)))
    }
    papers = sorted(set(found.values()) - excluded)

    def progress(column, result):
        print(
            f"split {column}: {result.targets[-1]} hidden, "
            f"reachable {result.reachable[-1]:.0%}, default {result.default_recall[-1]:.0%}, "
            f"citations {result.citation_recall[-1]:.0%}",
            flush=True,
        )

    result = sweep(conn, client, papers, args.splits, args.k, args.top, excluded, progress)
    means, worst = result.recall.mean(axis=1), result.recall.min(axis=1)
    print(f"\nmean recall@{args.k} over {args.splits} splits")
    print(f"  reachable        {np.mean(result.reachable):6.1%}")
    print(f"  citation count   {np.mean(result.citation_recall):6.1%}")
    print(f"  default weights  {np.mean(result.default_recall):6.1%}")
    print("\ntop 15 settings (mean, worst split)")
    for i in np.lexsort((-worst, -means))[:15]:
        print(f"  {means[i]:6.1%}  {worst[i]:6.1%}  {result.configs[i]}")
    print("\nbest setting for each venue weight")
    for value in GRID["venue"]:
        rows = [i for i, setting in enumerate(result.configs) if setting["venue"] == value]
        best = max(rows, key=lambda i: means[i])
        print(f"  venue={value:<4} {means[best]:6.1%}  {result.configs[best]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
