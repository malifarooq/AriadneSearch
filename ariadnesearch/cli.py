"""Command line entry point: `ariadne <command>`."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ariadnesearch import mine, references, tune, views
from ariadnesearch.config import Config, load_config
from ariadnesearch.db import cache_path, create_project, get_project_id, init_db
from ariadnesearch.prune import prune
from ariadnesearch.errors import AriadneError, NotFound
from ariadnesearch.expand import (
    PER_PAPER_LIMIT,
    UPDATE_CAP,
    add_seeds,
    add_seeds_from_bib,
    add_work_ids_as_seeds,
    expand_project,
    record_decision,
    update_project,
)
from ariadnesearch.identity import resolve
from ariadnesearch.ieee import IEEEClient, is_ieee_doi, open_store
from ariadnesearch.openalex import OpenAlexClient
from ariadnesearch.rank import load_snapshot, load_weights, rank
from ariadnesearch.records import normalize_doi
from ariadnesearch.venues import (
    CORE_EDITION,
    RANK_ORDER,
    download_core_csv,
    add_venue_papers,
    find_venues,
    import_core_csv,
    list_targets,
    load_targets,
    set_targets,
    set_venue_alias,
    unmatched_venues,
)

KNOWN_PAPER = "W2741809807"  # OpenAlex's documentation example
IEEE_CHECK_QUERY = "neural network"  # any query that IEEE-hosted papers match
DOCTOR_PAGE_CHECK = 101  # one more than a page, so the cursor must work


@dataclass
class App:
    config: Config
    conn: sqlite3.Connection
    client: OpenAlexClient


@contextmanager
def open_app() -> Iterator[App]:
    config = load_config()
    conn = init_db(config.graph_db)
    client = OpenAlexClient(
        conn, config.openalex_api_key, daily_usd=config.openalex_daily_usd
    )
    try:
        yield App(config, conn, client)
    finally:
        client.http.close()
        conn.close()


def _paper_id(conn: sqlite3.Connection, paper: str) -> int | None:
    """Read a paper ID as `rank` prints it, or a DOI."""
    paper = paper.strip()
    return int(paper) if paper.isdigit() else resolve(conn, "doi", normalize_doi(paper))


def _print_unresolved(items: list[str]) -> None:
    if items:
        print(f"Could not resolve {len(items)}:")
        for item in items:
            print(f"  {item}")


def cmd_init(_: argparse.Namespace) -> int:
    path = load_config().graph_db
    init_db(path).close()
    print(f"database ready: {path}")
    return 0


def _check_openalex(app: App) -> bool:
    """Run a lookup and two pages of citing papers. This costs about $0.0002."""
    record = app.client.work_by_id(KNOWN_PAPER)
    if record is None or not record.title:
        print("OpenAlex:     unexpected response to a lookup")
        return False
    print(f"OpenAlex:     lookup working ({record.n_refs} references on a test paper)")

    citers, total = app.client.fetch_citers([KNOWN_PAPER], DOCTOR_PAGE_CHECK)
    if len(citers) < min(DOCTOR_PAGE_CHECK, total):
        print(f"OpenAlex:     paging stopped early ({len(citers)} of {total})")
        return False
    with_authors = sum(bool(item.authors) for item in citers)
    with_abstract = sum(bool(item.abstract) for item in citers)
    print(
        f"OpenAlex:     paging working ({len(citers)} of {total} citing papers, "
        f"{with_authors} with authors, {with_abstract} with abstracts)"
    )
    return True


def _check_ieee(app: App) -> bool:
    """Run one search and one DOI lookup. This uses 2 daily IEEE calls."""
    store = open_store(app.config.ieee_db)
    ieee = IEEEClient(
        app.config.ieee_api_key,
        store,
        app.conn,
        daily_soft_limit=app.config.ieee_daily_soft_limit,
        rate_per_second=app.config.ieee_rate_per_second,
    )
    try:
        found = [
            item for item in ieee.search(IEEE_CHECK_QUERY, 5) if is_ieee_doi(item.doi)
        ]
        if not found:
            print("IEEE:         search returned no IEEE-hosted paper")
            return False
        records = ieee.lookup_dois([found[0].doi], refresh=True)
        if not records:
            print("IEEE:         DOI lookup returned nothing")
            return False
        print(
            f"IEEE:         working ({records[0].citing_paper_count} citations and "
            f"{len(records[0].ieee_terms)} IEEE terms on a test paper)"
        )
        return True
    finally:
        store.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check setup. IEEE is only called with --ieee."""
    with open_app() as app:
        print(f"Data folder:  {app.config.data_dir}")
        print(f"OpenAlex key: {'set' if app.config.openalex_api_key else 'NOT SET'}")
        print(f"IEEE key:     {'set' if app.config.ieee_api_key else 'NOT SET'}")
        rows = app.conn.execute("SELECT COUNT(*) FROM venue_rank").fetchone()[0]
        print(f"CORE rows:    {rows}")
        usage = views.usage_view(app.conn)
        print(
            f"Used today:   ${usage['openalex_cost_usd']:.4f} of "
            f"${app.config.openalex_daily_usd:.2f} OpenAlex, "
            f"{usage['ieee_calls']} of {app.config.ieee_daily_soft_limit} IEEE calls"
        )
        working = _check_openalex(app)
        if args.ieee:
            working = _check_ieee(app) and working
    return 0 if working else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("ariadnesearch.server:app", host="127.0.0.1", port=args.port)
    return 0


def cmd_project_create(args: argparse.Namespace) -> int:
    with open_app() as app:
        project_id = create_project(app.conn, args.name)
    print(f"created project {args.name} (ID {project_id})")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    if not (args.identifiers or args.bib or args.mine):
        print("Give at least one identifier, --bib, or --mine.", file=sys.stderr)
        return 2
    with open_app() as app:
        project_id = get_project_id(app.conn, args.project)
        added = 0
        unresolved: list[str] = []
        no_references: list[str] = []
        if args.identifiers:
            report = add_seeds(app.conn, app.client, project_id, args.identifiers)
            added += len(report.added)
            unresolved += report.unresolved
            no_references += report.no_references
        if args.bib:
            report = add_seeds_from_bib(app.conn, app.client, project_id, args.bib)
            added += len(report.added)
            unresolved += report.unresolved
            no_references += report.no_references
        if args.mine:
            profile = mine.interest_profile(app.conn)
            add_work_ids_as_seeds(app.conn, project_id, profile)
            added += len(profile)
    print(f"added {added} seeds")
    _print_unresolved(unresolved)
    if no_references:
        print(
            f"OpenAlex has no references for {len(no_references)}, so expanding "
            "follows only the papers that cite them:"
        )
        for title in no_references:
            print(f"  {title}")
    return 0


def cmd_expand(args: argparse.Namespace) -> int:
    with open_app() as app:
        project_id = get_project_id(app.conn, args.project)
        report = expand_project(
            app.conn,
            app.client,
            project_id,
            args.top,
            app.config.citer_cap,
            per_paper=args.per_paper or None,
        )
    print(
        f"hydrated {report.hydrated}, stored {report.citers_added} citers, "
        f"added {report.frontier_added} to the frontier, cost ${report.cost_usd:.4f}"
    )
    if report.cap_hit:
        print(f"citer cap reached on {len(report.cap_hit)} papers")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    with open_app() as app:
        project_id = get_project_id(app.conn, args.project)
        report = update_project(
            app.conn, app.client, project_id, args.since, args.cap, args.per_paper or None
        )
        new = set(report.new_papers)
        snapshot, state = load_snapshot(app.conn, project_id)
        ranked = [
            item
            for item in rank(snapshot, state, load_weights(app.conn, project_id))
            if item.work_id in new
        ][: args.k]
        summaries = views.work_summaries(app.conn, [item.work_id for item in ranked])
    print(
        f"{report.citers_seen} citing papers published since {report.since}, "
        f"{len(new)} new to this project, cost ${report.cost_usd:.4f}"
    )
    if report.capped:
        print(f"stopped at {args.cap} papers. Run `expand` for a full refresh.")
    for item in ranked:
        summary = summaries.get(item.work_id) or {}
        title = (summary.get("title") or "Untitled")[:78]
        print(f"{item.score:7.3f}  {item.work_id:>8}  {summary.get('year') or '':>4}  {title}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    with open_app() as app:
        files = [app.config.graph_db, cache_path(app.config.graph_db)]
        before = sum(path.stat().st_size for path in files if path.exists())
        report = prune(app.conn, vacuum=args.vacuum)
        after = sum(path.stat().st_size for path in files if path.exists())
    print(
        f"removed {report.papers} placeholder papers, {report.links} links and "
        f"{report.cache_entries} expired cache entries"
    )
    if args.vacuum:
        print(f"database files: {before / 2**20:.1f} MB before, {after / 2**20:.1f} MB after")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    def progress(column, result):
        print(
            f"split {column + 1} of {args.splits}: default weights find "
            f"{result.default_recall[-1]:.0%} of {result.targets[-1]} hidden seeds",
            flush=True,
        )

    with open_app() as app:
        project_id = get_project_id(app.conn, args.project)
        best, result = tune.tune_project(
            app.conn, app.client, project_id, args.splits, progress=progress
        )
    _, recall = result.best()
    print(f"saved weights {best}")
    print(
        f"hidden seeds found in the top 50: {recall:.0%} with these weights, "
        f"{sum(result.default_recall) / len(result.default_recall):.0%} with the defaults"
    )
    return 0


def cmd_refs(args: argparse.Namespace) -> int:
    if not (args.citations or args.arxiv):
        print("Give --citations FILE or --arxiv.", file=sys.stderr)
        return 2
    with open_app() as app:
        work_id = _paper_id(app.conn, args.paper)
        if not app.conn.execute("SELECT 1 FROM work WHERE id = ?", (work_id,)).fetchone():
            print(f"error: unknown paper {args.paper}. Add it as a seed first.", file=sys.stderr)
            return 1
        if args.arxiv:
            arxiv_id = app.conn.execute(
                "SELECT value FROM work_alias WHERE work_id = ? AND scheme = 'arxiv'", (work_id,)
            ).fetchone()
            if arxiv_id is None:
                print("error: this paper has no arXiv ID.", file=sys.stderr)
                return 1
            blob = references.fetch_arxiv_source(arxiv_id["value"])
            entries = references.entries_from_source(references.read_source(blob)) if blob else []
            source = "arxiv"
        elif args.bib:
            if args.citations.suffix.lower() == ".tex":
                keys = references.cited_keys(args.citations.read_text(errors="replace"))
            else:
                keys = mine.parse_citations(args.citations)
            bib = mine.parse_bib(args.bib)
            entries = [bib[key] for key in sorted(keys) if key in bib]
            source = "file"
        else:
            entries = references.entries_from_source(
                {args.citations.name: args.citations.read_text(encoding="utf-8", errors="replace")}
            )
            source = "file"
        stored, unresolved = references.attach_references(
            app.conn, app.client, work_id, entries, source
        )
    print(f"stored {stored} references from {len(entries)} entries")
    _print_unresolved(unresolved)
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    with open_app() as app:
        project_id = get_project_id(app.conn, args.project)
        view = views.frontier_view(
            app.conn,
            project_id,
            args.k,
            hide_cited=args.hide_cited,
            hide_library=args.hide_library,
            min_rank=args.min_rank,
            min_year=args.min_year,
            min_impact=args.min_impact,
            sort=args.sort,
        )
    items = view["items"]
    print(f"{len(items)} shown of {view['matching']} matching")
    print(f"{'score':>7}  {'id':>8}   CORE  2y-IF  paper")
    for item in items:
        rank = item["rank"]["rank"] if item["rank"] else "-"
        rank = rank if rank in RANK_ORDER or rank == "-" else "other"  # e.g. National
        impact = (item["journal"] or {}).get("mean_citedness")
        impact_text = f"{impact:.1f}" if impact is not None else "-"
        cited = " [cited by me]" if item["mine"]["cited_by_me"] else ""
        title = (item["title"] or "Untitled")[:72]
        print(
            f"{item['score']:7.3f}  {item['id']:>8}  {rank:>5}  {impact_text:>5}  "
            f"{item['label']}: {title}{cited}"
        )
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["score", "id", "title", "year", "venue", "core_rank",
                 "journal_2yr_citedness", "journal_h_index", "doi",
                 "cited_by", "cited_by_me", "in_library", "reasons"]
            )
            for item in items:
                writer.writerow(
                    [
                        item["score"],
                        item["id"],
                        item["title"],
                        item["year"],
                        item["venue"],
                        item["rank"]["rank"] if item["rank"] else "",
                        (item["journal"] or {}).get("mean_citedness", ""),
                        (item["journal"] or {}).get("h_index", ""),
                        item.get("doi", ""),
                        item["cited_by"],
                        item["mine"]["cited_by_me"],
                        item["mine"]["in_library"],
                        "; ".join(item["reasons"]),
                    ]
                )
        print(f"wrote {args.csv}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    with open_app() as app:
        project_id = get_project_id(app.conn, args.project)
        work_id = _paper_id(app.conn, args.paper)
        if work_id is None:
            raise NotFound(f"No paper with the ID or DOI {args.paper}.")
        record_decision(app.conn, project_id, work_id, args.decision)
    print(f"{args.paper}: {args.decision}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with open_app() as app:
        text = views.export_bib(app.conn, get_project_id(app.conn, args.project))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0


def _print_import(report: mine.ImportReport) -> None:
    print(f"stored {report.added}")
    _print_unresolved(report.unresolved)


def cmd_mine_sync(args: argparse.Namespace) -> int:
    with open_app() as app:
        papers = mine.load_my_papers(args.file)
        report = mine.sync_published(app.conn, app.client, papers)
    _print_import(report)
    return 0


def cmd_mine_draft(args: argparse.Namespace) -> int:
    with open_app() as app:
        report = mine.import_draft(
            app.conn, app.client, args.citations, args.bib, args.title
        )
    _print_import(report)
    return 0


def cmd_mine_list(args: argparse.Namespace) -> int:
    with open_app() as app:
        if args.title:
            for work in mine.cited_in(app.conn, args.title):
                title = (work["title"] or "Untitled")[:80]
                print(f"{work['year'] or '':>4}  {work['doi'] or '-':<34}  {title}")
            return 0
        for paper in mine.my_lists(app.conn):
            print(f"{paper['cited']:>4} cited  {paper['status']:<9}  {paper['title']}")
    return 0


def cmd_mine_rename(args: argparse.Namespace) -> int:
    with open_app() as app:
        mine.rename_list(app.conn, args.old, args.new)
    print(f"renamed to {args.new}")
    return 0


def cmd_mine_zotero(args: argparse.Namespace) -> int:
    with open_app() as app:
        report = mine.import_zotero(app.conn, app.client, args.bib)
    _print_import(report)
    return 0


def cmd_venue_import(args: argparse.Namespace) -> int:
    if not (args.csv or args.download):
        print("Give a CSV file, or --download to fetch one from the CORE portal.", file=sys.stderr)
        return 2
    with open_app() as app:
        path = args.csv
        if args.download:
            path = download_core_csv(args.edition or CORE_EDITION, app.config.data_dir)
            print(f"saved {path}")
        count = import_core_csv(app.conn, path, None if args.download else args.edition)
    print(f"imported {count} venue ranks")
    return 0


def cmd_venue_targets(args: argparse.Namespace) -> int:
    with open_app() as app:
        if args.clear:
            set_targets(app.conn, [])
        elif args.file:
            print(f"saved {set_targets(app.conn, load_targets(args.file))} target venues")
        targets = list_targets(app.conn)
    for target in targets:
        acronym = f" ({target['acronym']})" if target["acronym"] else ""
        print(f"{target['rank']:>2}  {target['title']}{acronym}")
    if not targets:
        print("no target venues. See targets.example.yaml")
    return 0


def cmd_venue_find(args: argparse.Namespace) -> int:
    with open_app() as app:
        venues = find_venues(app.conn, app.client, args.name)
    for venue in venues:
        print(f"{venue.id}  {venue.type or '-':<12} {venue.works_count or 0:>8}  {venue.name}")
    return 0


def cmd_venue_add(args: argparse.Namespace) -> int:
    with open_app() as app:
        project_id = get_project_id(app.conn, args.project)
        added = add_venue_papers(
            app.conn, app.client, project_id, args.source_id, args.since, args.cap
        )
    print(f"added {added} papers to the frontier")
    return 0


def cmd_venue_unmatched(args: argparse.Namespace) -> int:
    with open_app() as app:
        names = unmatched_venues(app.conn, get_project_id(app.conn, args.project))
    print("\n".join(names) if names else "no unmatched venues")
    return 0


def cmd_venue_map(args: argparse.Namespace) -> int:
    with open_app() as app:
        set_venue_alias(app.conn, args.raw_name, args.core_title)
    print("saved")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ariadne")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("init", help="create the database")
    p.set_defaults(handler=cmd_init)

    p = commands.add_parser("doctor", help="check keys and API connections")
    p.add_argument("--ieee", action="store_true", help="also check IEEE (uses 2 calls)")
    p.set_defaults(handler=cmd_doctor)

    p = commands.add_parser("serve", help="start the local web app")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(handler=cmd_serve)

    p = commands.add_parser("project", help="create a project")
    p.add_argument("name")
    p.set_defaults(handler=cmd_project_create)

    p = commands.add_parser("seed", help="add seed papers to a project")
    p.add_argument("project")
    p.add_argument("identifiers", nargs="*", help="DOIs, arXiv IDs or exact titles")
    p.add_argument("--bib", type=Path, help="add every entry in a .bib file")
    p.add_argument("--mine", action="store_true", help="add everything you have cited")
    p.set_defaults(handler=cmd_seed)

    p = commands.add_parser("expand", help="run one expansion round")
    p.add_argument("project")
    p.add_argument("--top", type=int, default=20, help="with --per-paper 0 only")
    p.add_argument(
        "--per-paper",
        type=int,
        default=PER_PAPER_LIMIT,
        help="suggestions each newly added paper brings in, 0 for every neighbour",
    )
    p.set_defaults(handler=cmd_expand)

    p = commands.add_parser("update", help="add new papers citing your known papers")
    p.add_argument("project")
    p.add_argument(
        "--since", type=date.fromisoformat, help="YYYY-MM-DD, default: last run less 14 days"
    )
    p.add_argument("--cap", type=int, default=UPDATE_CAP, help="most citing papers to fetch")
    p.add_argument(
        "--per-paper",
        type=int,
        default=PER_PAPER_LIMIT,
        help="new papers each known paper may add, 0 for no limit",
    )
    p.add_argument("-k", type=int, default=15, help="new papers to list")
    p.set_defaults(handler=cmd_update)

    p = commands.add_parser("prune", help="remove stored papers that can never be suggested")
    p.add_argument("--vacuum", action="store_true", help="also shrink the database files")
    p.set_defaults(handler=cmd_prune)

    p = commands.add_parser("tune", help="fit a project's ranking weights to its seeds")
    p.add_argument("project")
    p.add_argument("--splits", type=int, default=3, help="each one runs a full expansion")
    p.set_defaults(handler=cmd_tune)

    p = commands.add_parser("refs", help="give a paper the reference list OpenAlex lacks")
    p.add_argument("paper", help="paper ID or DOI")
    p.add_argument("--citations", type=Path, help=".bbl, or .tex/.aux with --bib")
    p.add_argument("--bib", type=Path)
    p.add_argument("--arxiv", action="store_true", help="read the paper's arXiv source")
    p.set_defaults(handler=cmd_refs)

    p = commands.add_parser("rank", help="list ranked suggestions")
    p.add_argument("project")
    p.add_argument("-k", type=int, default=50)
    p.add_argument("--csv", type=Path, help="write results to a CSV file")
    p.add_argument("--hide-cited", action="store_true")
    p.add_argument("--hide-library", action="store_true")
    p.add_argument("--min-rank", choices=["A*", "A", "B", "C"])
    p.add_argument("--min-year", type=int, help="recent papers only, such as 2025")
    p.add_argument("--min-impact", type=float, help="journal papers with this 2-year impact or more")
    p.add_argument(
        "--sort", choices=["relevance", "impact", "citations", "newest"], default="relevance"
    )
    p.set_defaults(handler=cmd_rank)

    p = commands.add_parser("decide", help="accept, reject or mark a paper read")
    p.add_argument("project")
    p.add_argument("paper", help="paper ID from `rank`, or a DOI")
    p.add_argument("decision", choices=["accepted", "rejected", "read"])
    p.set_defaults(handler=cmd_decide)

    p = commands.add_parser("export", help="export kept papers as BibTeX")
    p.add_argument("project")
    p.add_argument("--out", type=Path)
    p.set_defaults(handler=cmd_export)

    mine_parser = commands.add_parser("mine", help="import your own citations")
    mine_commands = mine_parser.add_subparsers(dest="mine_command", required=True)

    p = mine_commands.add_parser("sync", help="read my_papers.yaml")
    p.add_argument("--file", type=Path, default=Path("my_papers.yaml"))
    p.set_defaults(handler=cmd_mine_sync)

    p = mine_commands.add_parser("draft", help="save a draft's citations as a list")
    p.add_argument("title")
    p.add_argument("--citations", type=Path, required=True, help=".aux or .bbl file")
    p.add_argument("--bib", type=Path, help="needed with .aux, optional with .bbl")
    p.set_defaults(handler=cmd_mine_draft)

    p = mine_commands.add_parser("list", help="show your citation lists")
    p.add_argument("title", nargs="?", help="show the works this one cites")
    p.set_defaults(handler=cmd_mine_list)

    p = mine_commands.add_parser("rename", help="rename a citation list")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(handler=cmd_mine_rename)

    p = mine_commands.add_parser("zotero", help="import a Zotero BibTeX export")
    p.add_argument("bib", type=Path)
    p.set_defaults(handler=cmd_mine_zotero)

    venue_parser = commands.add_parser("venue", help="venue ranks and venue input")
    venue_commands = venue_parser.add_subparsers(dest="venue_command", required=True)

    p = venue_commands.add_parser("import", help="load an ICORE CSV export")
    p.add_argument("csv", type=Path, nargs="?")
    p.add_argument("--download", action="store_true", help="fetch the CSV from the CORE portal")
    p.add_argument(
        "--edition", help=f"such as {CORE_EDITION}: what to download, or the CSV's edition"
    )
    p.set_defaults(handler=cmd_venue_import)

    p = venue_commands.add_parser("targets", help="show or load your target venue list")
    p.add_argument("file", nargs="?", type=Path, help="a YAML list, such as targets.yaml")
    p.add_argument("--clear", action="store_true", help="remove every target venue")
    p.set_defaults(handler=cmd_venue_targets)

    p = venue_commands.add_parser("find", help="find a venue's OpenAlex source ID")
    p.add_argument("name")
    p.set_defaults(handler=cmd_venue_find)

    p = venue_commands.add_parser("add", help="add a venue's papers to the frontier")
    p.add_argument("project")
    p.add_argument("source_id", help="from `ariadne venue find`")
    p.add_argument("--since", type=int, required=True, help="earliest year")
    p.add_argument("--cap", type=int, default=200)
    p.set_defaults(handler=cmd_venue_add)

    p = venue_commands.add_parser("unmatched", help="list venues with no CORE match")
    p.add_argument("project")
    p.set_defaults(handler=cmd_venue_unmatched)

    p = venue_commands.add_parser("map", help="match a venue to a CORE title by hand")
    p.add_argument("raw_name")
    p.add_argument("core_title")
    p.set_defaults(handler=cmd_venue_map)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # Windows consoles lack some characters
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (AriadneError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
