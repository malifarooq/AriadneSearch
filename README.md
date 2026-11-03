# AriadneSearch

A local citation graph explorer. Start from papers you know, fetch what they cite
and what cites them, and triage ranked suggestions on an interactive map.

OpenAlex supplies the citation graph. IEEE Xplore adds details when you ask for
them. Everything you collect stays in the `data/` folder on your own machine.

## Setup

Requires Python 3.10 or newer and git.

```bash
git clone https://github.com/<owner>/AriadneSearch.git
cd AriadneSearch
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"

cp .env.example .env             # then add your API keys to .env
ariadne init
ariadne doctor                   # checks OpenAlex, costs about $0.0002
ariadne serve                    # open http://127.0.0.1:8000
```

Activate the virtual environment in each new terminal. On Linux and macOS, run
`chmod 600 .env` so only you can read your keys.

### API keys

- **OpenAlex** (required): a free key from https://openalex.org/settings/api.
  Without one, OpenAlex returns only the first 100 results of a list, so
  `doctor` and `expand` stop with an error. Each key has its own daily allowance.
- **IEEE Xplore** (optional): adds IEEE details on request. `ariadne doctor --ieee`
  checks it, using two of the key's daily calls.

### Running on another machine

The server listens only on the machine it runs on and has no login. To use it from
a laptop while it runs on a remote machine, keep it that way and open an SSH
tunnel from the laptop:

```bash
ssh -N -L 8000:127.0.0.1:8000 user@remote-host
```

Then open http://127.0.0.1:8000 on the laptop.

## Using the page

1. Create a project.
2. Add a few seed papers by DOI, arXiv ID or exact title.
3. Press **Update graph**. Each new paper brings in its 10 best-scoring
   neighbours: papers it cites, papers citing it, and papers sharing at least two
   citers or references with it.
4. Accept or reject suggestions. The graph keeps its shape, and the button counts
   the papers waiting. Press **Update graph** to add their neighbours and redraw.

Each added paper brings in its neighbours once. To change how many, run
`ariadne expand PROJECT --per-paper N`, or `--per-paper 0` to add every neighbour.

Keyboard shortcuts: `j` and `k` move through the list, `a` accepts, `r` rejects,
`m` marks as read, and `/` jumps to the search box.

### Lists

The buttons above the list choose what it shows:

- **Suggestions**: papers to review, best first, each with the reason it appeared.
  Filters hide papers you have cited or saved, set a minimum venue rank or journal
  impact, and sort by relevance, journal impact, citations or date.
- **Added papers**: only your seeds, accepted and read papers, in the list, the
  map and the counts.
- **State of the art**: suggestions published since a year you choose, drawn on
  the timeline by publication date.
- **Authors**: people who wrote two or more papers in the project, authors of your
  papers first. Clicking one highlights their papers. Names are matched as
  written, so one person can appear under two spellings.

### The graph

The map is a force-directed graph: papers push each other apart so none overlap,
labels appear as you zoom in, and hovering a paper dims everything not linked to it.

- **Find a paper** fades every paper that does not match the title, author, year
  or DOI you type, and lists matches from the whole project.
- **Colour** shows paper state, OpenAlex topic, or research area. In the topic
  views your papers are solid and papers to review are pale. The key lists the
  eight largest groups, and clicking one highlights its papers.
- **Size** shows importance in the paper's field (OpenAlex's citation percentile
  against papers of the same field and year, so new papers are not penalised for
  being new), raw citations, citations from papers in the project, or relevance.
  Sizes follow each paper's rank among the papers shown.
- **Timeline** runs left to right by publication date, and each year gets room for
  the papers it holds. Height is the same importance measure as size.

The strip below the graph shows today's API use.

## Keeping a project current

Press **Check new papers**, or run:

```bash
ariadne update PROJECT
```

This fetches papers that cite your seeds and accepted papers and were published
since the last run, less 14 days for papers OpenAlex indexes late. It asks about
100 known papers per call, so a weekly run costs a few calls. Each known paper adds
at most its 10 best new citers to the suggestions.

A paper indexed later than that window is missed, so now and then run it with an
earlier date:

```bash
ariadne update PROJECT --since YYYY-MM-DD
```

**Update graph** also refetches each known paper's citing papers once they are 30
days old. Those are stored and improve scores, but only `update` adds new citing
papers to the suggestions.

## Missing reference lists

OpenAlex has no references for some papers, often recent conference papers or ones
known mostly through arXiv. **Update graph** fills these for your added papers from
their arXiv source: the compiled `.bbl` if there is one, otherwise the `\cite` keys
in the `.tex` files matched to the `.bib`. The source is read in memory and never
unpacked or run. Each paper is tried once, at most five per run, and entries
without a DOI cost a title search each, capped at 25 per paper.

To fill one from your own files instead:

```bash
ariadne refs DOI --citations paper.bbl
ariadne refs DOI --citations main.tex --bib refs.bib
ariadne refs DOI --arxiv
```

## Ranking weights

A new project starts with neutral weights. Once it has at least 15 seed or accepted
papers, fit the weights to them with **Ranking weights → Fit to my seed papers** on
the page, or:

```bash
ariadne tune PROJECT
```

This hides a fifth of the seeds, grows a graph from the rest, and keeps the setting
that ranks the hidden seeds highest, out of about 1,000. It repeats that three
times, so expect several minutes and a few cents of OpenAlex use. The weights are
saved on that project only.

## Papers you have cited

Copy `my_papers.example.yaml` to `my_papers.yaml`, list your published papers with
their DOIs, then run:

```bash
ariadne mine sync
```

For a draft, use the compiled LaTeX files, because a `.bib` can hold entries the
draft never cites. A `.bbl` is enough on its own; an `.aux` needs the `.bib` too:

```bash
ariadne mine draft "Draft title" --citations paper.bbl
ariadne mine draft "Draft title" --citations paper.aux --bib refs.bib
```

Each draft is kept as a named list, and its papers are marked "Cited by me"
wherever they appear. Importing again under the same name adds to the list.

```bash
ariadne mine list                   # your lists and their sizes
ariadne mine list "Draft title"     # the papers in one list
ariadne mine rename "Draft title" "Final title"
```

Zotero items are tracked separately as "in my library". Export a collection as
BibTeX, then run `ariadne mine zotero library.bib`. To start a project from
everything you have cited, run `ariadne seed PROJECT --mine`.

## Venue ranks

### CORE

The CORE list ranks computer science conferences. Each user downloads it from the
CORE portal (https://portal.core.edu.au/conf-ranks/) into their own `data/` folder:

```bash
ariadne venue import --download                     # the default edition
ariadne venue import --download --edition CORE2023  # or a named one
ariadne venue unmatched PROJECT                     # venues with no match
ariadne venue map "Name in OpenAlex" "Title in the CORE list"
```

`ariadne venue import FILE.csv` loads an export saved by hand. The list belongs to
CORE, so it stays out of the repository: read CORE's usage terms before sharing it.

A rank shifts a paper's score by the `venue` weight (0.2 by default) times +0.5 for
A*, +0.25 for A, 0 for B and -0.25 for C. Venues without a rank, which includes
most journals and venues outside computer science, get 0, so a missing rank never
counts against a paper.

### Target venues

CORE's ranks may not fit your field, and it ranks no journals. List the venues
that matter to you with your own rank for each, and they replace CORE's rank
wherever they match: in scores, in the minimum-rank filter, and on labels, which
read "Target A*" instead of "CORE A*". Use **Target venues** on the page, or keep
the list in a file:

```bash
cp targets.example.yaml targets.yaml   # then edit it
ariadne venue targets targets.yaml     # load it, replacing the current list
ariadne venue targets                  # show the current list
```

### Journal impact

Each **Update graph** looks up the project's journals and shows each journal's
2-year mean citedness from OpenAlex next to its papers. This uses the impact factor
formula on OpenAlex's citation data. Clarivate's Journal Impact Factor is licensed
and has no public API, so the numbers will not match it exactly. Lookups cost one
call per 100 journals and are refreshed every 90 days. Journal impact does not
change scores, but suggestions can be filtered and sorted by it, on the page or
with `ariadne rank PROJECT --sort impact --min-impact 2`.

## IEEE Xplore

`ariadnesearch/ieee.py` lists the design rules for this adapter. IEEE content stays
in `data/ieee.db`, and only a user action triggers an IEEE call. These rules are one
reading of the IEEE API terms, so check with your institution how they apply to
your key.

## Checking the results

Before relying on the suggestions, check OpenAlex's coverage of your field with
about 50 papers you know, one DOI per line:

```bash
python eval/coverage_audit.py known_papers.txt
```

Then compare the ranker with sorting by citation count:

```bash
python eval/loo.py holdout refs.bib --exclude DOI_OF_THE_PAPER_THE_BIB_CAME_FROM
python eval/loo.py temporal
python eval/sweep.py refs.bib       # the full weight comparison behind `ariadne tune`
```

## Sharing and collaboration

Each person runs their own copy with their own keys in `.env`. API keys belong to
the person who requested them, so never share one.

The repository holds no personal data. Git ignores `.env`, `data/` (your database,
cached API responses, the CORE list and any files you import), `my_papers.yaml`
and `targets.yaml`. Keep it that way: the database holds your decisions, drafts and
citation lists.

The server has no login, so do not expose it to a network. To share a reading
list, use **Export BibTeX**, or `ariadne export PROJECT --out refs.bib`.

## Command reference

| Command | Job |
|---|---|
| `ariadne init` | Create the database |
| `ariadne doctor` | Check keys and API access |
| `ariadne serve` | Start the web page |
| `ariadne project NAME` | Create a project |
| `ariadne seed PROJECT DOI...` | Add seeds. Also takes `--bib` and `--mine` |
| `ariadne expand PROJECT` | Add new papers' best neighbours (the Update graph button) |
| `ariadne update PROJECT` | Add papers citing your known papers since the last run |
| `ariadne rank PROJECT` | List suggestions. Also takes `--csv`, `--sort` and filters |
| `ariadne decide PROJECT PAPER accepted` | Record a decision |
| `ariadne export PROJECT --out refs.bib` | Export kept papers |
| `ariadne tune PROJECT` | Fit the ranking weights to the project's seeds |
| `ariadne refs PAPER --citations FILE` | Give a paper the reference list OpenAlex lacks |
| `ariadne mine sync` | Import your published papers' citations |
| `ariadne mine draft TITLE --citations FILE` | Save a draft's citations as a list |
| `ariadne mine list [TITLE]` | Show your citation lists |
| `ariadne venue import --download` | Download and load the CORE list |
| `ariadne venue targets [FILE]` | Show or load your target venue list |
| `ariadne venue find NAME` | Find a venue's OpenAlex ID |
| `ariadne venue add PROJECT ID --since YEAR` | Add a venue's papers |

Every command has `--help`.

## Development

Run the tests with `pytest`. They use a fake OpenAlex and never call a real API, so
they need no keys. The GitHub workflow in `.github/workflows/tests.yml` runs them on
each push.

| File | Job |
|---|---|
| `ariadnesearch/openalex.py` | OpenAlex client, cache and budget guard |
| `ariadnesearch/identity.py` | Duplicate detection and reversible merges |
| `ariadnesearch/expand.py` | Graph growth within the API budget |
| `ariadnesearch/rank.py` | Scoring and "why this paper" evidence |
| `ariadnesearch/tune.py` | Ranking weights fitted to a project's seeds |
| `ariadnesearch/references.py` | Reference lists from arXiv sources or your files |
| `ariadnesearch/mine.py` | Your citations and library |
| `ariadnesearch/venues.py` | CORE ranks, target venues and venue input |
| `ariadnesearch/journals.py` | Journal impact lookup |
| `ariadnesearch/topics.py` | OpenAlex topics for colouring |
| `ariadnesearch/ieee.py` | On-demand IEEE details |
| `ariadnesearch/views.py` | Data shared by the web page and the CLI |
| `ariadnesearch/server.py` | Local API and web page host |
| `ariadnesearch/cli.py` | Terminal commands |
| `web/` | The web page |
| `eval/` | Coverage check and ranker evaluation |
| `tests/` | Tests |

## Licence

AriadneSearch is released under the MIT licence. See `LICENSE`.

`web/vendor/` holds force-graph 1.51.4 (MIT). `web/fonts/` holds Barlow Semi
Condensed and Source Serif 4 (SIL Open Font License). Their licences sit alongside
them.
