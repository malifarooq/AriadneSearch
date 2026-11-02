"""Check OpenAlex coverage on papers you know. Run this before trusting the graph.

Usage:
    python eval/coverage_audit.py known_papers.txt
    python eval/coverage_audit.py known_papers.txt --ieee

Input: one DOI per line, about 50 papers from your own area. Blank lines and
lines starting with # are ignored.

Reports:
    - papers missing from OpenAlex
    - papers with zero references, which are likely coverage gaps
    - with --ieee, IEEE citing_paper_count against OpenAlex cited_by_count

Run by hand only. IEEE comparisons are printed as aggregates, and per-paper IEEE
values stay in ieee.db.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ariadnesearch.config import load_config
from ariadnesearch.db import init_db
from ariadnesearch.errors import AriadneError
from ariadnesearch.ieee import IEEEClient, is_ieee_doi, open_store
from ariadnesearch.openalex import OpenAlexClient
from ariadnesearch.records import normalize_doi


@dataclass
class CoverageReport:
    total: int = 0
    missing: list[str] = field(default_factory=list)
    zero_references: list[str] = field(default_factory=list)
    prefix_counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    median_references: float | None = None
    ieee_compared: int = 0
    median_openalex_to_ieee_ratio: float | None = None


def read_dois(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        normalize_doi(line)
        for line in (raw.strip() for raw in lines)
        if line and not line.startswith("#")
    ]


def audit(
    dois: list[str], client: OpenAlexClient, ieee: IEEEClient | None = None
) -> CoverageReport:
    unique = sorted(set(dois))
    records = {record.doi: record for record in client.works_by_dois(unique)}
    report = CoverageReport(total=len(unique))
    report.missing = [doi for doi in unique if doi not in records]
    report.zero_references = sorted(
        doi for doi, record in records.items() if record.n_refs == 0
    )
    counts = [record.n_refs for record in records.values() if record.n_refs]
    report.median_references = statistics.median(counts) if counts else None

    wanted = Counter(doi.split("/", 1)[0] for doi in unique)
    found = Counter(doi.split("/", 1)[0] for doi in records)
    report.prefix_counts = {
        prefix: (found[prefix], total) for prefix, total in wanted.most_common()
    }

    if ieee is not None:
        ieee_records = ieee.lookup_dois([doi for doi in records if is_ieee_doi(doi)])
        ratios = [
            records[item.doi].cited_by_count / item.citing_paper_count
            for item in ieee_records
            if item.citing_paper_count
            and records[item.doi].cited_by_count is not None
        ]
        report.ieee_compared = len(ratios)
        report.median_openalex_to_ieee_ratio = (
            statistics.median(ratios) if ratios else None
        )
    return report


def print_report(report: CoverageReport) -> None:
    found = report.total - len(report.missing)
    print(f"Found in OpenAlex: {found} of {report.total}")
    print(f"Zero references:   {len(report.zero_references)} of {found}")
    if report.median_references is not None:
        print(f"Median references: {report.median_references:g}")
    print("By DOI prefix:")
    for prefix, (present, total) in report.prefix_counts.items():
        print(f"  {prefix}: {present} of {total}")
    if report.ieee_compared:
        print(
            f"OpenAlex/IEEE citation count, median of {report.ieee_compared}: "
            f"{report.median_openalex_to_ieee_ratio:.2f}"
        )
    if report.missing:
        print("Missing:")
        for doi in report.missing:
            print(f"  {doi}")
    if report.zero_references:
        print("Zero references:")
        for doi in report.zero_references:
            print(f"  {doi}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dois", type=Path, help="text file with one DOI per line")
    parser.add_argument("--ieee", action="store_true", help="compare with IEEE counts")
    args = parser.parse_args()

    config = load_config()
    conn = init_db(config.graph_db)
    client = OpenAlexClient(
        conn, config.openalex_api_key, daily_usd=config.openalex_daily_usd
    )
    ieee = None
    if args.ieee:
        ieee = IEEEClient(
            config.ieee_api_key,
            open_store(config.ieee_db),
            conn,
            daily_soft_limit=config.ieee_daily_soft_limit,
            rate_per_second=config.ieee_rate_per_second,
        )
    try:
        print_report(audit(read_dois(args.dois), client, ieee))
    except AriadneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
