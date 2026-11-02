"""Track what you have cited, and what is in your library.

Published papers: DOIs in my_papers.yaml, references from OpenAlex.
Drafts: `.aux` or `.bbl` files, because a `.bib` can hold entries you never cited.
Zotero: a separate "in my library" flag, because saved does not mean cited.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from ariadnesearch.db import chunks, utc_now
from ariadnesearch.errors import BadInput
from ariadnesearch.identity import (
    best_title_match,
    resolve_many,
    upsert_stubs,
    upsert_work,
)
from ariadnesearch.openalex import OpenAlexClient
from ariadnesearch.records import arxiv_doi, normalize_doi

_AUX_CITE = re.compile(r"\\citation\{([^}]*)\}")
_AUX_BIBLATEX = re.compile(r"\\abx@aux@cite\{(?:[^}]*\}\{)?([^}]*)\}")
_AUX_INPUT = re.compile(r"\\@input\{([^}]*)\}")
# The key may follow the label on the next line, after a % comment.
_BBL_ITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*(?:%[^\n]*\n\s*)*\{([^}]*)\}")
_BBL_ENTRY = re.compile(r"\\entry\{([^}]*)\}")
_BBL_TITLE_FIELDS = (r"\showarticletitle{", r"\bibinfo{title}{", r"\field{title}{")
_BBL_FALLBACK_TITLE = (r"\bibinfo{booktitle}{",)  # software and data entries
_BBL_ARXIV = re.compile(r"\\showeprint\[arxiv\]\{([^}]*)\}|\\field\{eprint\}\{([^}]*)\}", re.I)
_LATEX_ACCENT = re.compile(r"\\['`^\"~=.]\s*\{?([A-Za-z])\}?")
_LATEX_LETTER_ACCENT = re.compile(r"\\[uvHc]\s*\{([A-Za-z])\}")
_QUOTED_TITLE = re.compile(r"``(.+?)''", re.DOTALL)  # IEEEtran style
_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+\*?")
_BIB_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
_BIB_SKIPPED = {"comment", "string", "preamble"}
_DOI_IN_TEXT = re.compile(r"10\.\d{4,9}/[^\s\"'<>{}]+")
_ARXIV_IN_TEXT = re.compile(
    r"(?:arxiv[:.\s]*(?:org/(?:abs|pdf)/)?|arxiv\.org/(?:abs|pdf)/)"
    r"([0-9]{4}\.[0-9]{4,5}|[a-z.\-]+/[0-9]{7})",
    re.IGNORECASE,
)


@dataclass
class MyPaperEntry:
    title: str
    doi: str | None
    status: str  # published or draft


@dataclass
class BibEntry:
    key: str
    title: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None


@dataclass
class MyFlags:
    cited_by_me: bool = False
    cited_in: list[str] = field(default_factory=list)
    last_cited_year: int | None = None
    in_library: bool = False


@dataclass
class ImportReport:
    added: int = 0
    unresolved: list[str] = field(default_factory=list)


def load_my_papers(path: Path) -> list[MyPaperEntry]:
    """Read my_papers.yaml. This uses DOIs, not author-name matching."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    papers = []
    for item in data.get("papers") or []:
        status = item.get("status", "published")
        if status not in ("published", "draft"):
            raise BadInput(f"Unknown status in {path.name}: {status}")
        if not item.get("title"):
            raise BadInput(f"Every paper in {path.name} needs a title.")
        papers.append(MyPaperEntry(str(item["title"]), item.get("doi"), status))
    return papers


def _my_paper_id(
    conn: sqlite3.Connection,
    title: str,
    doi: str | None,
    year: int | None,
    status: str,
) -> int:
    row = conn.execute(
        "SELECT id FROM my_paper WHERE title = ? OR (doi IS NOT NULL AND doi = ?)",
        (title, doi),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE my_paper SET doi = COALESCE(?, doi), year = COALESCE(?, year), "
            "status = ? WHERE id = ?",
            (doi, year, status, row["id"]),
        )
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO my_paper(title, doi, year, status) VALUES (?, ?, ?, ?)",
        (title, doi, year, status),
    )
    return int(cursor.lastrowid)


def _add_citations(
    conn: sqlite3.Connection, my_paper_id: int, work_ids: set[str], source: str
) -> int:
    conn.executemany(
        "INSERT OR IGNORE INTO my_citation(my_paper_id, work_id, source) "
        "VALUES (?, ?, ?)",
        [(my_paper_id, work_id, source) for work_id in work_ids],
    )
    return len(work_ids)


def sync_published(
    conn: sqlite3.Connection, client: OpenAlexClient, papers: list[MyPaperEntry]
) -> ImportReport:
    """Store the references of your published papers."""
    report = ImportReport()
    for paper in papers:
        if paper.status != "published":
            continue
        record = client.work_by_doi(paper.doi) if paper.doi else None
        if record is None:
            report.unresolved.append(paper.doi or paper.title)
            continue
        upsert_work(conn, record)
        cited = set(upsert_stubs(conn, record.referenced_ids).values())
        my_id = _my_paper_id(conn, paper.title, normalize_doi(paper.doi), record.year, "published")
        report.added += _add_citations(conn, my_id, cited, "openalex")
        conn.commit()
    return report


def parse_aux(path: Path) -> set[str]:
    """Return the citation keys actually used in a LaTeX `.aux` file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    keys: set[str] = set()
    for match in _AUX_CITE.finditer(text):
        keys.update(key.strip() for key in match.group(1).split(","))
    keys.update(match.group(1).strip() for match in _AUX_BIBLATEX.finditer(text))
    for match in _AUX_INPUT.finditer(text):
        included = path.parent / match.group(1)
        if included.is_file():
            keys |= parse_aux(included)
    return {key for key in keys if key and key != "*"}


def parse_bbl(path: Path) -> set[str]:
    """Return the citation keys in a compiled `.bbl` file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    keys = {match.group(1).strip() for match in _BBL_ITEM.finditer(text)}
    keys |= {match.group(1).strip() for match in _BBL_ENTRY.finditer(text)}
    return {key for key in keys if key}


def _latex_text(text: str) -> str:
    """Turn a LaTeX title into plain text."""
    text = _LATEX_ACCENT.sub(r"\1", text)
    text = _LATEX_LETTER_ACCENT.sub(r"\1", text)
    text = text.replace("\\&", "&").replace("~", " ").replace("$", "")
    text = _LATEX_COMMAND.sub(" ", text)
    text = re.sub(r"[{}]", "", text)
    return " ".join(text.split()).strip(" .,")


def _braced_after(text: str, marker: str) -> str | None:
    start = text.find(marker)
    if start < 0:
        return None
    value, _ = _entry_body(text, start + len(marker))
    return value


def _bbl_title(body: str) -> str | None:
    """Find an entry's title: tagged fields, IEEEtran quotes, then plain layout."""
    for marker in _BBL_TITLE_FIELDS:
        value = _braced_after(body, marker)
        if value:
            return value
    quoted = _QUOTED_TITLE.search(body)
    if quoted:
        return quoted.group(1)
    blocks = body.split("\\newblock")
    if len(blocks) > 2 and "\\bibinfo" not in blocks[1]:
        return blocks[1]  # plain styles: authors, then title, then venue
    for marker in _BBL_FALLBACK_TITLE:
        value = _braced_after(body, marker)
        if value:
            return value
    return None


def parse_bbl_entries(path: Path) -> dict[str, BibEntry]:
    """Read each cited work's title, DOI and arXiv ID from a compiled `.bbl` file.

    This covers natbib-style `\\bibitem` lists, including ACM's tagged fields, and
    biblatex `\\entry` blocks, so a draft can be imported without its `.bib`.
    """
    return parse_bbl_text(path.read_text(encoding="utf-8", errors="replace"))


def parse_bbl_text(text: str) -> dict[str, BibEntry]:
    """Like `parse_bbl_entries`, for text already read."""
    starts = sorted(
        [*_BBL_ITEM.finditer(text), *_BBL_ENTRY.finditer(text)],
        key=lambda match: match.start(),
    )
    entries: dict[str, BibEntry] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[match.end() : end]
        key = match.group(1).strip()
        title = _bbl_title(body)

        doi = None
        found = _DOI_IN_TEXT.search(body)
        if found:
            doi = normalize_doi(found.group(0).rstrip(".,;"))

        arxiv_id = None
        eprint = _BBL_ARXIV.search(body)
        if eprint and (eprint.group(1) or "eprinttype}{arxiv" in body.lower()):
            arxiv_id = (eprint.group(1) or eprint.group(2)).strip()
        elif found_arxiv := _ARXIV_IN_TEXT.search(body):
            arxiv_id = found_arxiv.group(1)

        entries[key] = BibEntry(key, _latex_text(title) if title else None, doi, arxiv_id)
    return entries


def parse_citations(path: Path) -> set[str]:
    """Read citation keys from an `.aux` or `.bbl` file."""
    return parse_bbl(path) if path.suffix.lower() == ".bbl" else parse_aux(path)


def _entry_body(text: str, start: int) -> tuple[str, int]:
    """Return the text of a brace-delimited entry from `start`."""
    depth = 1
    position = start
    while position < len(text) and depth:
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
        position += 1
    return text[start : position - 1], position


def _bib_fields(body: str) -> dict[str, str]:
    """Parse `name = {value}`, `name = "value"` and `name = 123` pairs."""
    fields: dict[str, str] = {}
    position = 0
    pattern = re.compile(r"\s*,?\s*([A-Za-z][\w-]*)\s*=\s*")
    while True:
        match = pattern.match(body, position)
        if match is None:
            next_comma = body.find(",", position)
            if next_comma < 0:
                return fields
            position = next_comma + 1
            continue
        name = match.group(1).lower()
        position = match.end()
        if position >= len(body):
            return fields
        opener = body[position]
        if opener == "{":
            value, position = _entry_body(body, position + 1)
        elif opener == '"':
            end = position + 1
            while end < len(body) and (
                body[end] != '"' or body[end - 1] == "\\"
            ):
                end += 1
            value = body[position + 1 : end]
            position = end + 1
        else:
            end = body.find(",", position)
            end = len(body) if end < 0 else end
            value = body[position:end]
            position = end
        fields[name] = " ".join(value.split())


def _clean_title(title: str | None) -> str | None:
    if not title:
        return None
    return " ".join(re.sub(r"[{}]", "", title).split()) or None


def _bib_identifiers(fields: dict[str, str]) -> tuple[str | None, str | None]:
    doi = None
    for name in ("doi", "url", "note", "howpublished"):
        match = _DOI_IN_TEXT.search(fields.get(name, ""))
        if match:
            doi = normalize_doi(match.group(0).rstrip(".,;"))
            break

    arxiv_id = None
    prefix = fields.get("archiveprefix", fields.get("eprinttype", "")).lower()
    if prefix == "arxiv" and fields.get("eprint"):
        arxiv_id = fields["eprint"].strip()
    else:
        for name in ("journal", "url", "note", "eprint", "howpublished"):
            match = _ARXIV_IN_TEXT.search(fields.get(name, ""))
            if match:
                arxiv_id = match.group(1)
                break
    return doi, arxiv_id


def parse_bib(path: Path) -> dict[str, BibEntry]:
    """Map `.bib` keys to identifiers, so cited keys can be resolved."""
    return parse_bib_text(path.read_text(encoding="utf-8", errors="replace"))


def parse_bib_text(text: str) -> dict[str, BibEntry]:
    """Like `parse_bib`, for text already read."""
    entries: dict[str, BibEntry] = {}
    position = 0
    while True:
        match = _BIB_ENTRY.search(text, position)
        if match is None:
            return entries
        body, position = _entry_body(text, match.end())
        if match.group(1).lower() in _BIB_SKIPPED:
            continue
        fields = _bib_fields(body)
        doi, arxiv_id = _bib_identifiers(fields)
        key = match.group(2)
        entries[key] = BibEntry(key, _clean_title(fields.get("title")), doi, arxiv_id)


def resolve_entries(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    entries: list[BibEntry],
    max_searches: int | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Find works for bibliography entries. Return ({key: work ID}, unresolved).

    Entries without a DOI or arXiv ID cost a title search each, at ten times a
    list call. `max_searches` caps those, and the rest count as unresolved.
    """
    wanted: dict[str, str] = {}
    for entry in entries:
        doi = entry.doi or (arxiv_doi(entry.arxiv_id) if entry.arxiv_id else None)
        if doi:
            wanted[entry.key] = normalize_doi(doi)

    known = resolve_many(conn, "doi", list(wanted.values()))
    missing = sorted(set(wanted.values()) - set(known))
    for record in client.works_by_dois(missing):
        if record.doi:
            known[record.doi] = upsert_work(conn, record)
    conn.commit()

    found = {key: known[doi] for key, doi in wanted.items() if doi in known}
    unresolved: list[str] = []
    searches = 0
    for entry in entries:
        if entry.key in found:
            continue
        searchable = entry.title and (max_searches is None or searches < max_searches)
        searches += 1 if searchable else 0
        match = (
            best_title_match(client.search(entry.title, per_page=5), entry.title)
            if searchable
            else None
        )
        if match is None:
            unresolved.append(entry.key)
        else:
            found[entry.key] = upsert_work(conn, match)
            conn.commit()
    return found, unresolved


def import_draft(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    citations_path: Path,
    bib_path: Path | None,
    title: str,
) -> ImportReport:
    """Store a draft's citations as a permanent list named `title`.

    Pass an `.aux` or `.bbl` with its `.bib`. A `.bbl` alone also works, because it
    holds each cited work's title and usually its DOI. Re-importing under the same
    title adds to the list.
    """
    keys = parse_citations(citations_path)
    if bib_path is not None:
        bib = parse_bib(bib_path)
    elif citations_path.suffix.lower() == ".bbl":
        bib = parse_bbl_entries(citations_path)
    else:
        raise BadInput("An .aux file only lists keys. Pass the .bib with --bib.")
    report = ImportReport(unresolved=sorted(keys - set(bib)))
    entries = [bib[key] for key in sorted(keys & set(bib))]
    found, unresolved = resolve_entries(conn, client, entries)
    report.unresolved.extend(unresolved)
    my_id = _my_paper_id(conn, title, None, date.today().year, "draft")
    report.added = _add_citations(conn, my_id, set(found.values()), "aux")
    conn.commit()
    return report


def my_lists(conn: sqlite3.Connection) -> list[dict]:
    """Return your papers and drafts, each with how many works it cites."""
    rows = conn.execute(
        """
        SELECT p.id, p.title, p.status, p.year, COUNT(DISTINCT c.work_id) AS cited
        FROM my_paper p LEFT JOIN my_citation c ON c.my_paper_id = p.id
        GROUP BY p.id ORDER BY p.year DESC, p.title
        """
    )
    return [dict(row) for row in rows]


def rename_list(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Rename one of your papers or drafts."""
    new = new.strip()
    if not new:
        raise BadInput("The new title cannot be empty.")
    if conn.execute("SELECT 1 FROM my_paper WHERE title = ?", (new,)).fetchone():
        raise BadInput(f"A list titled {new} already exists.")
    cursor = conn.execute("UPDATE my_paper SET title = ? WHERE title = ?", (new, old))
    if cursor.rowcount == 0:
        raise BadInput(f"No paper or draft titled: {old}")
    conn.commit()


def cited_in(conn: sqlite3.Connection, title: str) -> list[dict]:
    """Return the works one of your papers or drafts cites."""
    paper = conn.execute("SELECT id FROM my_paper WHERE title = ?", (title,)).fetchone()
    if paper is None:
        raise BadInput(f"No paper or draft titled: {title}")
    rows = conn.execute(
        """
        SELECT DISTINCT w.id, w.title, w.year,
            (SELECT value FROM work_alias a WHERE a.work_id = w.id AND a.scheme = 'doi'
             ORDER BY value LIKE '10.48550/%' LIMIT 1) AS doi
        FROM my_citation c JOIN work w ON w.id = c.work_id
        WHERE c.my_paper_id = ?
        ORDER BY w.year, w.title
        """,
        (paper["id"],),
    )
    return [dict(row) for row in rows]


def import_zotero(
    conn: sqlite3.Connection, client: OpenAlexClient, bib_path: Path
) -> ImportReport:
    """Mark papers from a Zotero BibTeX export as in your library."""
    entries = list(parse_bib(bib_path).values())
    found, unresolved = resolve_entries(conn, client, entries)
    now = utc_now()
    conn.executemany(
        "INSERT OR IGNORE INTO library_item(work_id, source, added_at) "
        "VALUES (?, 'zotero', ?)",
        [(work_id, now) for work_id in set(found.values())],
    )
    conn.commit()
    return ImportReport(len(set(found.values())), unresolved)


def annotate(conn: sqlite3.Connection, work_ids: list[str]) -> dict[str, MyFlags]:
    """Return the "previously cited by me" fields for a set of papers."""
    flags = {work_id: MyFlags() for work_id in work_ids}
    for chunk in chunks(work_ids, 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.work_id, p.title, p.year
            FROM my_citation c JOIN my_paper p ON p.id = c.my_paper_id
            WHERE c.work_id IN ({marks})
            ORDER BY p.year DESC
            """,
            chunk,
        )
        for row in rows:
            flag = flags[row["work_id"]]
            flag.cited_by_me = True
            flag.cited_in.append(row["title"])
            if row["year"] and row["year"] > (flag.last_cited_year or 0):
                flag.last_cited_year = row["year"]
        rows = conn.execute(
            f"SELECT DISTINCT work_id FROM library_item WHERE work_id IN ({marks})",
            chunk,
        )
        for row in rows:
            flags[row["work_id"]].in_library = True
    return flags


def often_cited_with_mine(conn: sqlite3.Connection, k: int = 50) -> list[str]:
    """Return stored papers often cited alongside yours that you have never cited.

    This uses citers already in the database, so results grow with expansion.
    """
    rows = conn.execute(
        """
        SELECT other.dst AS work_id, COUNT(DISTINCT other.src) AS shared
        FROM my_citation mine
        JOIN cites citing ON citing.dst = mine.work_id
        JOIN cites other ON other.src = citing.src
        WHERE other.dst NOT IN (SELECT work_id FROM my_citation)
        GROUP BY other.dst
        ORDER BY shared DESC, other.dst
        LIMIT ?
        """,
        (k,),
    )
    return [row["work_id"] for row in rows]


def interest_profile(conn: sqlite3.Connection) -> set[str]:
    """Return your cited papers, as the default interest profile for ranking."""
    rows = conn.execute("SELECT DISTINCT work_id FROM my_citation")
    return {row["work_id"] for row in rows}
