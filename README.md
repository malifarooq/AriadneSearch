# AriadneSearch

A local citation graph explorer. Start from papers you know, fetch what they cite
and what cites them, and triage ranked suggestions on a visual map.

OpenAlex supplies the graph. IEEE Xplore adds details when you ask for them. Your
data stays in `data/` on your machine.

## Setup on Linux

Requires Python 3.10 or newer.

```bash
# Debian or Ubuntu only, if venv is missing
sudo apt install python3-venv

git clone https://github.com/OWNER/AriadneSearch.git
cd AriadneSearch
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

cp .env.example .env
chmod 600 .env
nano .env                      # add your API keys

ariadne init
pytest
ariadne doctor                # checks OpenAlex, costs about $0.0002
ariadne doctor --ieee         # also checks IEEE, uses 2 daily calls
ariadne serve                 # http://127.0.0.1:8000
```

Get a free OpenAlex key at https://openalex.org/settings/api. Without one, OpenAlex
returns only the first 100 results of a list, so `doctor` and `expand` stop with an
error. The IEEE key is optional.

Run `source .venv/bin/activate` in each new terminal.

The server only listens on your own machine. To open it from another computer,
keep it that way and use an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 you@linux-machine
```

Then open http://127.0.0.1:8000 on that computer.

## First run

1. Open the page, then create a project.
2. Add a few seed papers by DOI, arXiv ID or exact title.
3. Press **Update graph**. Each new paper brings in its 10 best-scoring neighbours:
   papers it cites, papers citing it, and papers sharing at least two citers or
   references with it.
4. Accept or reject suggestions. The graph keeps its shape, and the button counts
   the papers waiting. Press **Update graph** to add their neighbours and redraw.

Each added paper brings in its neighbours once. Change the number with
`ariadne expand PROJECT --per-paper N`, or use `--per-paper 0` to add every
neighbour as earlier versions did.

Above the list, **Added papers** shows only your seeds, accepted and read papers,
in the list, the map and the counts. **State of the art** shows suggestions
published since a year you pick, best first, and draws them on the timeline by
publication date. **Suggestions** switches back.

The map is a force-directed graph: papers push each other apart so none overlap,
labels appear as you zoom in, and hovering a paper dims everything not linked to
it. The size menu sets what node size shows: importance in its field (OpenAlex's
citation percentile against papers of the same field and year, so new papers are
not penalised for being new), raw citations, citations from papers in this
project, or relevance to you. Sizes follow each paper's rank among those shown.
The timeline runs left to right by publication date, and each year gets room for
the papers it holds. Height on the timeline is the same importance measure as node
size: more important papers sit higher.

**Find a paper** above the graph (or press `/`) fades every paper that does not
match the title, author, year or DOI you type, and lists matches from the whole
project. Enter or a click jumps to the paper. Escape clears it.

The colour menu colours papers by their OpenAlex topic or research area (subfield)
instead of by state. Your papers are solid and papers to review are pale. The key
lists the eight largest groups, and clicking one highlights its papers.

**Authors** lists people who wrote two or more papers in the project, authors of
your papers first. Clicking one highlights their papers and lists them. Names are
matched as written, so one person can appear under two spellings.

Keyboard shortcuts: `j` and `k` move, `a` accepts, `r` rejects, `m` marks as read.

Each suggestion says why it appeared, and the strip below the map shows today's
API use.

## Missing reference lists

OpenAlex has no references for some papers, often recent conference papers or
ones known mostly through arXiv. **Update graph** fills these for your added papers
from their arXiv source: the compiled `.bbl` if there is one, otherwise the `\cite`
keys in the `.tex` files matched to the `.bib`. The source is read in memory and
never unpacked or run. Each paper is tried once, at most five per run, and entries
without a DOI cost a title search each, capped at 25 per paper.

To fill one from your own files instead:

```bash
ariadne refs DOI --citations paper.bbl
ariadne refs DOI --citations main.tex --bib refs.bib
ariadne refs DOI --arxiv
```

## Weekly updates

Once a project has a graph, press **Check new papers**, or run:

```bash
ariadne update PROJECT
```

This fetches papers that cite your seeds and accepted papers and were published
since the last run, less 14 days for papers OpenAlex indexes late. It asks about
100 known papers per call, so it stays cheap. For 107 known papers, two weeks of
new papers took 14 calls and 14 seconds. Each known paper adds at most its 10
best new citers to the suggestions, and the command lists them.

`update` only looks back 14 days before its last run, so a paper OpenAlex indexes
later than that is missed. Once a month, run it with an earlier date to catch
those:

```bash
ariadne update PROJECT --since 2026-09-01
```

**Update graph** also refetches each known paper's citing papers once they are
30 days old. Those are stored and improve scores, but only `update` adds new
citing papers to the suggestions.

## Before relying on it

Run the coverage check on about 50 papers you know, with one DOI per line:

```bash
python eval/coverage_audit.py known_papers.txt
```

Then compare the ranker with sorting by citation count:

```bash
python eval/loo.py holdout refs.bib --exclude DOI_OF_THE_PAPER_THE_BIB_CAME_FROM
python eval/loo.py temporal
```

## Ranking weights

A new project starts with neutral weights. Once it has at least 15 seed or
accepted papers, fit the weights to them:

```bash
ariadne tune PROJECT
```

or open **Ranking weights** on the page and press **Fit to my seed papers**. It
hides a fifth of your seeds, grows a graph from the rest, and keeps the setting
that ranks the hidden seeds highest, out of about 1,000. It repeats that three
times, which means three full expansions: expect several minutes and a few cents
of OpenAlex use. The weights are saved on that project only.

`python eval/sweep.py refs.bib` runs the same comparison on any bibliography and
prints the detail: the citation-count baseline, how many hidden papers were
reachable at all, and the best setting for each venue weight.

## Previously cited by me

Copy `my_papers.example.yaml` to `my_papers.yaml`, list your papers with their
DOIs, then run:

```bash
ariadne mine sync
```

Drafts use the compiled LaTeX files, because a `.bib` can hold entries you never
cited. A `.bbl` holds each work's title and usually its DOI, so it is enough on
its own. An `.aux` needs the `.bib` too:

```bash
ariadne mine draft "Draft title" --citations paper.bbl
ariadne mine draft "Draft title" --citations paper.aux --bib refs.bib
```

Each draft is kept as a named list. Its papers are marked "Cited by me" wherever
they appear, and importing again under the same name adds to the list:

```bash
ariadne mine list                     # your lists and their sizes
ariadne mine list "Draft title"       # the papers in one list
ariadne mine rename "Draft title" "Final title"
```

Zotero items are tracked separately as "in my library". Export a collection as
BibTeX, then run:

```bash
ariadne mine zotero library.bib
```

To seed a project from everything you have cited:

```bash
ariadne seed PROJECT --mine
```

## CORE venue ranks

Each person downloads the ICORE list from the CORE portal
(https://portal.core.edu.au/conf-ranks/) into their own `data/` folder:

```bash
ariadne venue import --download                    # the newest edition, ICORE2026
ariadne venue import --download --edition CORE2023 # or an older one
ariadne venue unmatched PROJECT
ariadne venue map "Name in OpenAlex" "Title in the CORE list"
```

`ariadne venue import FILE.csv` loads an export you saved yourself. The list is
CORE's, so it stays out of the repository: read CORE's usage document before
sharing it.

CORE covers computer science conferences, so most journals and physics venues
have no rank. A rank shifts a paper's score by the `venue` weight (default 0.2)
times +0.5 for A*, +0.25 for A, 0 for B and -0.25 for C. Venues CORE does not
rank, such as most journals, and CORE's national or regional labels get 0, so a
missing rank never counts against a paper. Set `venue` to 0 in a project's weights to use ranks as
labels and filters only.

## Target venues

CORE's ranks may not fit your field, and it ranks no journals. List the venues that
matter to you, with your own rank for each, and they replace CORE's rank wherever
they match: in scores, in the minimum-rank filter and on labels, which read
"Target A*" instead of "CORE A*". Use **Target venues** on the page, or keep the
list in a file:

```bash
cp targets.example.yaml targets.yaml   # then edit it
ariadne venue targets targets.yaml    # load it, replacing the current list
ariadne venue targets                 # show the current list
```

`targets.yaml` is yours, so git ignores it.

## Journal impact

After each `expand`, AriadneSearch looks up the journals in the project and shows each
journal's 2-year mean citedness from OpenAlex next to its papers. This uses the
impact factor formula on OpenAlex's citation data. Clarivate's Journal Impact
Factor is licensed and has no public API, so the numbers will not match it
exactly. Lookups cost one list call per 100 journals and are refreshed every 90
days. Journal impact does not change scores, but suggestions can be filtered by a
minimum journal impact and sorted by it, by citations or by newest, on the page or
with `ariadne rank PROJECT --sort impact --min-impact 2`.

## Command reference

| Command | Job |
|---|---|
| `ariadne project NAME` | Create a project |
| `ariadne seed PROJECT DOI...` | Add seeds. Also takes `--bib` and `--mine` |
| `ariadne expand PROJECT` | Add new papers' best neighbours (the Update graph button) |
| `ariadne update PROJECT` | Add papers citing your known papers since the last run |
| `ariadne refs PAPER --citations FILE` | Give a paper the reference list OpenAlex lacks |
| `ariadne rank PROJECT` | List suggestions. Also takes `--csv`, `--sort` and filters |
| `ariadne tune PROJECT` | Fit the ranking weights to the project's seed papers |
| `ariadne mine draft TITLE --citations FILE` | Save a draft's citations as a list |
| `ariadne mine list [TITLE]` | Show your citation lists |
| `ariadne decide PROJECT PAPER accepted` | Record a decision |
| `ariadne export PROJECT --out refs.bib` | Export kept papers |
| `ariadne venue import --download` | Download and load the CORE list |
| `ariadne venue targets [FILE]` | Show or load your target venue list |
| `ariadne venue find NAME` | Find a venue's OpenAlex ID |
| `ariadne venue add PROJECT ID --since 2022` | Add a venue's papers |

## IEEE

`ariadnesearch/ieee.py` lists the design rules for this adapter. IEEE content stays in
`data/ieee.db`, and only a user action triggers an IEEE call. These rules are one
reading of the IEEE API terms, so confirm with your library how they apply to
your key.

## Sharing with your lab

Each person runs their own copy: clone the repository, make a virtual
environment, and add their own keys to `.env`. An OpenAlex key is free and each
key has its own $1 daily allowance. IEEE keys belong to one person under IEEE's
terms, so never share one.

Nothing personal is in the repository. `.env`, `data/` (your database, cached API
responses, the CORE list and any `.bbl` you import) and `my_papers.yaml` are
ignored by git. Keep it that way: the database holds your decisions, drafts and
citation lists.

The server answers only on your own machine and has no login, so do not open it
to the network. To share a reading list, export it with **Export BibTeX**.

Ranking weights start neutral and are fitted per project with `ariadne tune`,
so each labmate's projects follow their own seed papers.

## Code map

| File | Job |
|---|---|
| `ariadnesearch/openalex.py` | OpenAlex client, cache and budget guard |
| `ariadnesearch/identity.py` | Duplicate detection and reversible merges |
| `ariadnesearch/expand.py` | Graph growth within the API budget |
| `ariadnesearch/rank.py` | Scoring and "why this paper" evidence |
| `ariadnesearch/mine.py` | Your citations and library |
| `ariadnesearch/venues.py` | CORE ranks and venue input |
| `ariadnesearch/journals.py` | Journal impact lookup |
| `ariadnesearch/topics.py` | OpenAlex topics for colouring |
| `ariadnesearch/references.py` | Reference lists from arXiv sources or your files |
| `ariadnesearch/tune.py` | Ranking weights fitted to a project's seeds |
| `ariadnesearch/ieee.py` | On-demand IEEE details |
| `ariadnesearch/views.py` | Data shared by the web page and the CLI |
| `ariadnesearch/server.py` | Local API and web page host |
| `ariadnesearch/cli.py` | Terminal commands |
| `web/` | The web page |
| `eval/` | Coverage check and ranker evaluation |
| `tests/` | Tests, using a fake OpenAlex |

## Licence

AriadneSearch is released under the MIT licence. See `LICENSE`.

## Third-party files

`web/vendor/` holds force-graph 1.51.4 (MIT). `web/fonts/` holds Barlow Semi Condensed
and Source Serif 4 (SIL Open Font License). Their licences sit alongside them.
