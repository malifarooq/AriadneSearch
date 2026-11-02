import gzip
import io
import tarfile

from conftest import FakeOpenAlex, make_client, work
from ariadnesearch.expand import add_seeds, expand_project
from ariadnesearch.identity import resolve
from ariadnesearch.references import (
    attach_references,
    cited_keys,
    entries_from_source,
    fill_from_arxiv,
    read_source,
)

TEX = r"""
\section{Intro}
LUT networks~\cite{logicnets, polylut} and readout \citep[see][]{readout}.
% \cite{commented_out}
\nocite{*}
"""
BIB = """
@inproceedings{logicnets, title={Matched filtering foundations}, doi={10.1000/ref.10}}
@inproceedings{polylut, title={Superconducting qubit measurement}, doi={10.1000/ref.11}}
@article{readout, title={Dispersive readout theory}, doi={10.1000/cocited.30}}
@article{unused, title={Never cited}, doi={10.1000/other.50}}
"""


def tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_cited_keys_skip_comments_and_wildcards():
    assert cited_keys(TEX) == {"logicnets", "polylut", "readout"}


def test_entries_prefer_a_bbl_then_cite_keys_with_the_bib():
    from_bib = entries_from_source({"main.tex": TEX, "refs.bib": BIB})
    assert {entry.key for entry in from_bib} == {"logicnets", "polylut", "readout"}
    bbl = "\\bibitem{only}\n\\newblock \\showarticletitle{Only one}.\n"
    assert [e.key for e in entries_from_source({"main.tex": TEX, "refs.bib": BIB, "main.bbl": bbl})] == ["only"]


def test_read_source_reads_bundles_and_single_files_only_in_memory():
    files = read_source(tarball({"main.tex": TEX, "refs.bib": BIB, "fig.png": "x"}))
    assert set(files) == {"main.tex", "refs.bib"}
    single = gzip.compress(b"\\documentclass{article}\n\\cite{a}")
    assert set(read_source(single)) == {"main.tex"}
    assert read_source(b"%PDF-1.5 not a source bundle") == {}


def test_attach_references_links_the_paper(conn, client, project):
    paper = add_seeds(conn, client, project, ["10.1109/seed.1"]).added[0]
    entries = entries_from_source({"main.tex": TEX, "refs.bib": BIB})
    stored, unresolved = attach_references(conn, client, paper, entries, "file")
    assert stored == 3 and unresolved == []
    target = resolve(conn, "doi", "10.1000/cocited.30")
    row = conn.execute("SELECT sources FROM cites WHERE src = ? AND dst = ?", (paper, target)).fetchone()
    assert row["sources"] == "file"


def test_update_graph_fills_a_missing_reference_list_from_arxiv(conn, project, monkeypatch):
    fake = FakeOpenAlex(
        [
            work(70, "Attention Is All You Need", 2017, (), 0, "10.48550/arxiv.1706.03762", arxiv="1706.03762"),
            *[work(n, t, 2010, (), 5, d) for n, t, d in [
                (10, "Matched filtering foundations", "10.1000/ref.10"),
                (11, "Superconducting qubit measurement", "10.1000/ref.11"),
                (30, "Dispersive readout theory", "10.1000/cocited.30"),
            ]],
        ]
    )
    client = make_client(conn, fake)
    paper = add_seeds(conn, client, project, ["10.48550/arxiv.1706.03762"]).added[0]
    requests = []

    def fake_fetch(arxiv_id):
        requests.append(arxiv_id)
        return tarball({"main.tex": TEX, "refs.bib": BIB})

    monkeypatch.setattr("ariadnesearch.references.fetch_arxiv_source", fake_fetch)
    report = expand_project(conn, client, project)
    assert requests == ["1706.03762"]
    assert report.refs_filled == 1
    assert conn.execute("SELECT n_refs FROM work WHERE id = ?", (paper,)).fetchone()[0] == 3
    expand_project(conn, client, project)
    assert fill_from_arxiv(conn, client, [paper]) == 0
    assert requests == ["1706.03762"]  # never asked twice
