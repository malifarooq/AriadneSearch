"""Reference lists for papers OpenAlex lists without any.

OpenAlex sometimes has no references for a paper, often a recent conference paper
or one known mostly through arXiv. The paper's own bibliography fills the gap: a
`.bbl`, an inline `thebibliography`, or `\\cite` keys matched to a `.bib`. It comes
from a file you supply, or from the paper's arXiv source, which is read in memory
and never unpacked to disk or run.
"""

from __future__ import annotations

import gzip
import io
import re
import sqlite3
import tarfile
import time
from collections.abc import Callable, Iterable

import httpx

from ariadnesearch import __version__
from ariadnesearch.db import utc_now
from ariadnesearch.errors import UpstreamError
from ariadnesearch.mine import BibEntry, parse_bbl_text, parse_bib_text, resolve_entries
from ariadnesearch.openalex import OpenAlexClient

ARXIV_SOURCE = "https://arxiv.org/e-print/{}"
ARXIV_DELAY_S = 3.0  # arXiv asks for no more than one request every three seconds
ARXIV_PER_RUN = 5  # papers filled from arXiv in one Update graph
TITLE_SEARCHES = 25  # per paper, since each costs ten list calls
MAX_SOURCE_BYTES = 50 * 2**20
MAX_FILE_BYTES = 5 * 2**20
SOURCE_SUFFIXES = (".tex", ".bbl", ".bib")

# \cite, \citep, \citet, \parencite, \autocite, \Cite, \nocite and friends.
_CITE = re.compile(r"\\(?:[A-Za-z]*[cC]ite[A-Za-z]*|nocite)\*?(?:\[[^\]]*\]){0,2}\{([^}]*)\}")
_COMMENT = re.compile(r"(?<!\\)%.*")

_last_arxiv_request = 0.0


def cited_keys(tex: str) -> set[str]:
    """Return the citation keys used in LaTeX source, ignoring comments."""
    body = _COMMENT.sub("", tex)
    keys = {key.strip() for match in _CITE.finditer(body) for key in match.group(1).split(",")}
    return {key for key in keys if key and key != "*"}


def entries_from_source(files: dict[str, str]) -> list[BibEntry]:
    """Return the works a paper cites, from its LaTeX files (name to text).

    A compiled `.bbl` is used first, then a bibliography written into the `.tex`,
    then the `\\cite` keys found in the `.tex` files matched to the `.bib` files.
    """
    def collect(names: Iterable[str]) -> dict[str, BibEntry]:
        entries: dict[str, BibEntry] = {}
        for name in names:
            entries.update(parse_bbl_text(files[name]))
        return entries

    compiled = collect(name for name in files if name.endswith(".bbl"))
    if compiled:
        return list(compiled.values())
    inline = collect(name for name in files if name.endswith(".tex") and "\\bibitem" in files[name])
    if inline:
        return list(inline.values())
    keys: set[str] = set()
    bib: dict[str, BibEntry] = {}
    for name, text in files.items():
        if name.endswith(".tex"):
            keys |= cited_keys(text)
        elif name.endswith(".bib"):
            bib.update(parse_bib_text(text))
    return [bib[key] for key in sorted(keys) if key in bib]


def read_source(blob: bytes) -> dict[str, str]:
    """Read the `.tex`, `.bbl` and `.bib` files of an arXiv source bundle, in memory."""
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
            total = 0
            for member in tar:
                if not member.isfile() or not member.name.endswith(SOURCE_SUFFIXES):
                    continue
                if member.size > MAX_FILE_BYTES or total + member.size > MAX_SOURCE_BYTES:
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    files[member.name] = handle.read().decode("utf-8", errors="replace")
                    total += member.size
        return files
    except tarfile.TarError:
        pass
    try:  # a single gzipped file, which is one .tex
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as handle:
            text = handle.read(MAX_FILE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {}  # arXiv has only a PDF for this paper
    return {"main.tex": text} if "\\documentclass" in text else {}


def fetch_arxiv_source(
    arxiv_id: str,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes | None:
    """Download a paper's arXiv source bundle, at arXiv's requested pace."""
    global _last_arxiv_request
    wait = ARXIV_DELAY_S - (time.monotonic() - _last_arxiv_request)
    if wait > 0:
        sleep(wait)
    client = http or httpx.Client(
        timeout=60, headers={"User-Agent": f"AriadneSearch/{__version__}"}, follow_redirects=True
    )
    try:
        response = client.get(ARXIV_SOURCE.format(arxiv_id))
    except httpx.TransportError as exc:
        raise UpstreamError(f"arXiv did not respond for {arxiv_id}.") from exc
    finally:
        _last_arxiv_request = time.monotonic()
        if http is None:
            client.close()
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise UpstreamError(f"arXiv returned HTTP {response.status_code} for {arxiv_id}.")
    return response.content if len(response.content) <= MAX_SOURCE_BYTES else None


def attach_references(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    work_id: str,
    entries: list[BibEntry],
    source: str,
) -> tuple[int, list[str]]:
    """Store `entries` as the works `work_id` cites. Return (stored, unresolved keys)."""
    found, unresolved = resolve_entries(conn, client, entries, max_searches=TITLE_SEARCHES)
    targets = sorted(set(found.values()) - {work_id})
    now = utc_now()
    conn.executemany(
        "INSERT OR IGNORE INTO cites(src, dst, sources, first_seen) VALUES (?, ?, ?, ?)",
        [(work_id, target, source, now) for target in targets],
    )
    conn.execute(
        "UPDATE work SET n_refs = MAX(COALESCE(n_refs, 0), ?) WHERE id = ?",
        (len(targets), work_id),
    )
    conn.execute(
        "INSERT OR REPLACE INTO ref_import(work_id, source, found, at) VALUES (?, ?, ?, ?)",
        (work_id, source, len(targets), now),
    )
    conn.commit()
    return len(targets), unresolved


def fill_from_arxiv(
    conn: sqlite3.Connection,
    client: OpenAlexClient,
    work_ids: Iterable[str],
    limit: int = ARXIV_PER_RUN,
) -> int:
    """Fill missing reference lists from arXiv. Return how many papers gained some.

    Only papers with no references, an arXiv ID and no earlier attempt are tried,
    so each costs at most one arXiv request ever.
    """
    filled = 0
    tried = 0
    for work_id in sorted(set(work_ids)):
        if tried >= limit:
            break
        row = conn.execute(
            """
            SELECT a.value FROM work w JOIN work_alias a ON a.work_id = w.id
            WHERE w.id = ? AND COALESCE(w.n_refs, 0) = 0 AND a.scheme = 'arxiv'
            AND NOT EXISTS (SELECT 1 FROM ref_import r WHERE r.work_id = w.id)
            """,
            (work_id,),
        ).fetchone()
        if row is None:
            continue
        tried += 1
        blob = fetch_arxiv_source(row["value"])
        entries = entries_from_source(read_source(blob)) if blob else []
        stored, _ = attach_references(conn, client, work_id, entries, "arxiv")
        filled += 1 if stored else 0
    return filled
