from pathlib import Path

import pytest

from ariadnesearch.errors import BadInput
from ariadnesearch.expand import add_seeds, expand_project
from ariadnesearch.identity import resolve
from ariadnesearch.mine import (
    MyPaperEntry,
    annotate,
    cited_in,
    import_draft,
    import_zotero,
    interest_profile,
    load_my_papers,
    my_lists,
    often_cited_with_mine,
    parse_aux,
    parse_bbl,
    parse_bbl_entries,
    parse_bib,
    parse_citations,
    rename_list,
    sync_published,
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_my_papers(tmp_path):
    path = write(
        tmp_path / "mine.yaml",
        "papers:\n  - title: One\n    doi: 10.1/a\n  - title: Two\n    status: draft\n",
    )
    papers = load_my_papers(path)
    assert papers[0] == MyPaperEntry("One", "10.1/a", "published")
    assert papers[1].status == "draft"


@pytest.mark.parametrize("text", ["papers:\n  - doi: 10.1/a\n", "papers:\n  - title: A\n    status: x\n"])
def test_load_my_papers_rejects_bad_entries(tmp_path, text):
    with pytest.raises(BadInput):
        load_my_papers(write(tmp_path / "mine.yaml", text))


def test_parse_aux_reads_keys_and_included_files(tmp_path):
    write(tmp_path / "chapter.aux", r"\citation{gamma}")
    aux = write(
        tmp_path / "main.aux",
        "\\citation{alpha,beta}\n\\citation{*}\n\\@input{chapter.aux}\n"
        "\\abx@aux@cite{0}{delta}\n\\bibstyle{ieeetr}\n",
    )
    assert parse_aux(aux) == {"alpha", "beta", "gamma", "delta"}


def test_parse_bbl_handles_bibtex_and_biblatex(tmp_path):
    bbl = write(
        tmp_path / "main.bbl",
        "\\bibitem{one} A\n\\bibitem[Smith(2020)]{two} B\n\\entry{three}{article}{}\n",
    )
    assert parse_bbl(bbl) == {"one", "two", "three"}
    assert parse_citations(bbl) == {"one", "two", "three"}


ACM_BBL = r"""
\bibitem[Andronic and Constantinides(2023)]%
        {andronic:2023:polylut}
\bibfield{author}{\bibinfo{person}{Marta Andronic}.} \bibinfo{year}{2023}\natexlab{}.
\newblock \showarticletitle{{PolyLUT}: Learning Piecewise Polynomials}. In \bibinfo{booktitle}{\emph{Proc. FPT}}.
\newblock
\href{https://doi.org/10.1109/ICFPT59805.2023.00012}{doi:\nolinkurl{10.1109/ICFPT59805.2023.00012}}

\bibitem[Hoang et~al\mbox{.}(2026b)]%
        {hoang:2026:kanele}
\newblock \showarticletitle{{KANEL\'E}: {Kolmogorov--Arnold} Networks}. In \bibinfo{booktitle}{X}.

\bibitem[Fahim(2021)]%
        {fahim:2021:hls4ml}
\newblock \showarticletitle{hls4ml: A Workflow}. In \bibinfo{booktitle}{\emph{TinyML}}.
\newblock
\showeprint[arxiv]{2103.05579}~[cs.LG]

\bibitem[anonymous(2026)]%
        {tool-repo}
\newblock \bibinfo{booktitle}{\emph{Example Software Flow}}.
\end{thebibliography}
"""

PLAIN_BBL = r"""
\bibitem{plain1}
A.~Author.
\newblock Matched filters in practice.
\newblock {\em Journal}, 1960.

\bibitem{ieee1}
A.~Author, ``Rapid readout of {Qubits},'' \emph{Phys. Rev.}, 2018.

\entry{bl1}{article}{}
  \field{title}{A biblatex title}
  \field{eprint}{2401.00001}
  \field{eprinttype}{arxiv}
\endentry
"""


def test_parse_bbl_entries_reads_titles_and_identifiers(tmp_path):
    entries = parse_bbl_entries(write(tmp_path / "acm.bbl", ACM_BBL))
    assert set(entries) == {
        "andronic:2023:polylut", "hoang:2026:kanele", "fahim:2021:hls4ml", "tool-repo"
    }
    polylut = entries["andronic:2023:polylut"]
    assert polylut.title == "PolyLUT: Learning Piecewise Polynomials"
    assert polylut.doi == "10.1109/icfpt59805.2023.00012"
    assert entries["hoang:2026:kanele"].title == "KANELE: Kolmogorov--Arnold Networks"
    assert entries["fahim:2021:hls4ml"].arxiv_id == "2103.05579"
    assert entries["tool-repo"].title == "Example Software Flow"

    other = parse_bbl_entries(write(tmp_path / "other.bbl", PLAIN_BBL))
    assert other["plain1"].title == "Matched filters in practice"
    assert other["ieee1"].title == "Rapid readout of Qubits"
    assert other["bl1"].title == "A biblatex title"
    assert other["bl1"].arxiv_id == "2401.00001"


def test_a_bbl_alone_makes_a_permanent_citation_list(conn, client, tmp_path):
    bbl = write(
        tmp_path / "draft.bbl",
        "\\bibitem[A(2005)]%\n  {mf}\n\\newblock \\showarticletitle{Matched filtering foundations}.\n"
        "\\href{https://doi.org/10.1000/ref.10}{x}\n"
        "\\bibitem[B(2010)]%\n  {dr}\n\\newblock \\showarticletitle{Dispersive readout theory}.\n"
        "\\bibitem[C(2026)]%\n  {repo}\n\\newblock \\bibinfo{booktitle}{Some software}.\n",
    )
    report = import_draft(conn, client, bbl, None, "Example draft")
    assert report.added == 2
    assert report.unresolved == ["repo"]
    assert [paper["cited"] for paper in my_lists(conn)] == [2]
    assert {work["year"] for work in cited_in(conn, "Example draft")} == {2005, 2010}
    work_id = resolve(conn, "doi", "10.1000/ref.10")
    assert annotate(conn, [work_id])[work_id].cited_in == ["Example draft"]

    assert import_draft(conn, client, bbl, None, "Example draft").added == 2
    assert [paper["cited"] for paper in my_lists(conn)] == [2]  # no duplicates
    rename_list(conn, "Example draft", "Example paper")
    assert annotate(conn, [work_id])[work_id].cited_in == ["Example paper"]
    with pytest.raises(BadInput):
        rename_list(conn, "No such list", "x")


def test_an_aux_needs_a_bib(conn, client, tmp_path):
    with pytest.raises(BadInput, match="bib"):
        import_draft(conn, client, write(tmp_path / "d.aux", r"\citation{a}"), None, "D")


BIB = r"""
@string{ieee = "IEEE"}
@comment{ignored, entry}
@article{plain,
  title = {A {Nested} Title: {FPGA} Readout},
  author = "Lovelace, Ada and Turing, Alan",
  doi = {10.1109/ABC.2024.1},
  year = 2024,
  month = jan,
}
@misc{urldoi, title = "From a URL", url = {https://doi.org/10.1145/xyz.9}}
@misc{eprint, title = {An eprint}, eprint = {2401.00001}, archivePrefix = {arXiv}}
@misc{biblatex, title = {A biblatex eprint}, eprint = {2402.00002}, eprinttype = {arxiv}}
@article{journalarxiv, title = {Scholar style}, journal = {arXiv preprint arXiv:1706.03762}}
@misc{urlarxiv, title = {URL style}, url = {https://arxiv.org/abs/2403.00003v2}}
@inproceedings{concat, title = "Joined" # " value", doi = {10.1000/after.concat}}
@book{nothing, title = {Only a title}}
"""


def test_parse_bib_finds_identifiers_in_each_style(tmp_path):
    entries = parse_bib(write(tmp_path / "refs.bib", BIB))

    assert entries["plain"].title == "A Nested Title: FPGA Readout"
    assert entries["plain"].doi == "10.1109/abc.2024.1"
    assert entries["urldoi"].doi == "10.1145/xyz.9"
    assert entries["eprint"].arxiv_id == "2401.00001"
    assert entries["biblatex"].arxiv_id == "2402.00002"
    assert entries["journalarxiv"].arxiv_id == "1706.03762"
    assert entries["urlarxiv"].arxiv_id == "2403.00003"
    assert entries["concat"].doi == "10.1000/after.concat"
    assert entries["nothing"].doi is None
    assert "ieee" not in entries
    assert "ignored" not in entries


def test_sync_published_stores_your_citations(conn, client):
    papers = [
        MyPaperEntry("Mine", "10.1109/seed.1", "published"),
        MyPaperEntry("Draft", None, "draft"),
        MyPaperEntry("Gone", "10.9999/none", "published"),
    ]
    report = sync_published(conn, client, papers)
    assert report.added == 3
    assert report.unresolved == ["10.9999/none"]

    flags = annotate(conn, [resolve(conn, "openalex", "W10")])
    flag = next(iter(flags.values()))
    assert flag.cited_by_me
    assert flag.cited_in == ["Mine"]
    assert flag.last_cited_year == 2022
    assert not flag.in_library


def test_sync_is_repeatable(conn, client):
    papers = [MyPaperEntry("Mine", "10.1109/seed.1", "published")]
    sync_published(conn, client, papers)
    sync_published(conn, client, papers)
    assert conn.execute("SELECT COUNT(*) FROM my_paper").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM my_citation").fetchone()[0] == 3


def test_draft_uses_only_keys_actually_cited(conn, client, tmp_path):
    aux = write(tmp_path / "d.aux", r"\citation{used,missing}")
    bib = write(
        tmp_path / "d.bib",
        "@article{used, title={U}, doi={10.1000/ref.10}}\n"
        "@article{unused, title={N}, doi={10.1000/ref.11}}\n",
    )
    report = import_draft(conn, client, aux, bib, "My draft")
    assert report.added == 1
    assert report.unresolved == ["missing"]
    assert annotate(conn, [resolve(conn, "doi", "10.1000/ref.10")])
    assert resolve(conn, "doi", "10.1000/ref.11") is None


def test_library_does_not_count_as_cited(conn, client, tmp_path):
    bib = write(tmp_path / "z.bib", "@article{a, title={Z}, doi={10.1000/ref.10}}\n")
    assert import_zotero(conn, client, bib).added == 1
    work_id = resolve(conn, "doi", "10.1000/ref.10")
    flag = annotate(conn, [work_id])[work_id]
    assert flag.in_library
    assert not flag.cited_by_me


def test_often_cited_with_mine_excludes_what_you_cited(conn, client, project):
    sync_published(conn, client, [MyPaperEntry("Mine", "10.1109/seed.1", "published")])
    add_seeds(conn, client, project, ["10.1109/seed.1", "10.1145/seed.2"])
    expand_project(conn, client, project)

    suggestions = often_cited_with_mine(conn)
    assert resolve(conn, "openalex", "W13") in suggestions
    assert not set(suggestions) & interest_profile(conn)


def test_title_searches_can_be_capped(conn, client):
    from ariadnesearch.mine import BibEntry, resolve_entries

    entries = [
        BibEntry("a", "Dispersive readout theory"),
        BibEntry("b", "Matched filtering foundations"),
        BibEntry("c", None, doi="10.1000/ref.11"),
    ]
    found, unresolved = resolve_entries(conn, client, entries, max_searches=1)
    assert set(found) == {"a", "c"}
    assert unresolved == ["b"]
