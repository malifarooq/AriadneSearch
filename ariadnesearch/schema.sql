-- Main database: data/graph.db
-- IEEE content is stored separately in data/ieee.db. See ieee.py.

-- Papers. `id` is internal and never an OpenAlex ID, because versions of one
-- document can appear there as separate records.
CREATE TABLE IF NOT EXISTS work (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    title_key   TEXT,     -- normalized title, used to find other versions
    authors     TEXT,     -- JSON list of names
    year        INTEGER,
    pub_date    TEXT,
    type        TEXT,
    venue       TEXT,
    venue_id    TEXT,     -- OpenAlex source ID
    venue_type  TEXT,
    abstract    TEXT,
    oa_cited_by INTEGER,
    oa_pct      REAL,
    n_refs      INTEGER,  -- 0 means unknown, not "cites nothing"
    topic       TEXT,     -- OpenAlex primary topic, '' when it has none
    subfield    TEXT,
    field       TEXT,
    level       TEXT NOT NULL DEFAULT 'stub'
                CHECK (level IN ('stub', 'hydrated', 'expanded'))
);
CREATE INDEX IF NOT EXISTS idx_work_title_key ON work(title_key);

-- Every external identifier for a paper.
CREATE TABLE IF NOT EXISTS work_alias (
    scheme  TEXT NOT NULL,  -- doi, openalex, arxiv, ieee
    value   TEXT NOT NULL,
    work_id TEXT NOT NULL REFERENCES work(id),
    PRIMARY KEY (scheme, value)
);
CREATE INDEX IF NOT EXISTS idx_alias_work ON work_alias(work_id);

-- Merges are reversible: snapshot_json holds what undo_merge needs to restore.
CREATE TABLE IF NOT EXISTS merge_log (
    id            INTEGER PRIMARY KEY,
    keep_id       TEXT NOT NULL,
    drop_id       TEXT NOT NULL,
    rule          TEXT NOT NULL,
    confidence    REAL NOT NULL,
    snapshot_json TEXT NOT NULL,
    ts            TEXT NOT NULL,
    undone_at     TEXT
);

-- Citation edges: src cites dst.
CREATE TABLE IF NOT EXISTS cites (
    src        TEXT NOT NULL REFERENCES work(id),
    dst        TEXT NOT NULL REFERENCES work(id),
    sources    TEXT NOT NULL DEFAULT 'openalex',
    first_seen TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS idx_cites_dst ON cites(dst);

-- Links between separate documents, such as a journal extension of a
-- conference paper.
CREATE TABLE IF NOT EXISTS relation (
    a    TEXT NOT NULL REFERENCES work(id),
    b    TEXT NOT NULL REFERENCES work(id),
    kind TEXT NOT NULL,
    PRIMARY KEY (a, b, kind)
);

CREATE TABLE IF NOT EXISTS project (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    weights_json TEXT NOT NULL DEFAULT '{}'
);

-- Triage state is per project: a paper can matter to one topic and not another.
CREATE TABLE IF NOT EXISTS membership (
    project_id INTEGER NOT NULL REFERENCES project(id),
    work_id    TEXT NOT NULL REFERENCES work(id),
    state      TEXT NOT NULL
               CHECK (state IN ('seed', 'frontier', 'accepted', 'rejected', 'read')),
    x          REAL,
    y          REAL,
    ts         TEXT NOT NULL,
    PRIMARY KEY (project_id, work_id)
);
CREATE INDEX IF NOT EXISTS idx_membership_state ON membership(project_id, state);

-- Append-only history of decisions and expansions.
CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER REFERENCES project(id),
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- Known papers whose best neighbours have been added. See expand._populate.
CREATE TABLE IF NOT EXISTS populated (
    project_id INTEGER NOT NULL REFERENCES project(id),
    work_id    TEXT NOT NULL REFERENCES work(id),
    at         TEXT NOT NULL,
    PRIMARY KEY (project_id, work_id)
);

-- Reference lists filled in from outside OpenAlex. See references.py.
CREATE TABLE IF NOT EXISTS ref_import (
    work_id TEXT PRIMARY KEY REFERENCES work(id),
    source  TEXT NOT NULL,     -- arxiv or file
    found   INTEGER NOT NULL,  -- references stored, 0 when nothing was found
    at      TEXT NOT NULL
);

-- How far each paper has been fetched.
CREATE TABLE IF NOT EXISTS fetch_state (
    work_id       TEXT PRIMARY KEY REFERENCES work(id),
    citers_total  INTEGER,
    refs_at       TEXT,
    citers_at     TEXT
);

-- Daily API use. Holds counts only, so it is safe to keep IEEE counts here.
CREATE TABLE IF NOT EXISTS budget (
    source   TEXT NOT NULL,
    day      TEXT NOT NULL,
    calls    INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (source, day)
);

-- OpenAlex responses, so tables can be rebuilt when merge logic changes.
-- Strip the API key from `url` before storing it. Never store IEEE responses here.
CREATE TABLE IF NOT EXISTS raw_response (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    body       BLOB NOT NULL,  -- zlib-compressed JSON
    fetched_at TEXT NOT NULL
);

-- Your own papers and what they cite.
CREATE TABLE IF NOT EXISTS my_paper (
    id     INTEGER PRIMARY KEY,
    title  TEXT NOT NULL,
    doi    TEXT,
    year   INTEGER,
    status TEXT NOT NULL CHECK (status IN ('published', 'draft'))
);

CREATE TABLE IF NOT EXISTS my_citation (
    my_paper_id INTEGER NOT NULL REFERENCES my_paper(id),
    work_id     TEXT NOT NULL REFERENCES work(id),
    source      TEXT NOT NULL CHECK (source IN ('openalex', 'aux', 'manual')),
    PRIMARY KEY (my_paper_id, work_id, source)
);
CREATE INDEX IF NOT EXISTS idx_my_citation_work ON my_citation(work_id);

-- Saved in your library. This does not mean cited.
CREATE TABLE IF NOT EXISTS library_item (
    work_id  TEXT NOT NULL REFERENCES work(id),
    source   TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (work_id, source)
);

-- Venue ranks, imported from a manual ICORE CSV export.
CREATE TABLE IF NOT EXISTS venue_rank (
    edition TEXT NOT NULL,  -- ICORE2026, CORE2023
    title   TEXT NOT NULL,
    acronym TEXT,
    rank    TEXT NOT NULL,
    PRIMARY KEY (edition, title)
);

-- Journal impact from OpenAlex, refreshed every 90 days. See journals.py.
CREATE TABLE IF NOT EXISTS venue_stats (
    venue_id       TEXT PRIMARY KEY,  -- OpenAlex source ID
    mean_citedness REAL,              -- 2-year mean citedness
    h_index        INTEGER,
    fetched_at     TEXT NOT NULL
);

-- Your own list of venues and ranks, which take precedence over CORE.
CREATE TABLE IF NOT EXISTS venue_target (
    title   TEXT PRIMARY KEY,
    acronym TEXT,
    rank    TEXT NOT NULL CHECK (rank IN ('A*', 'A', 'B', 'C'))
);

-- Link from a source venue name to a ranked title.
CREATE TABLE IF NOT EXISTS venue_alias (
    raw_name TEXT PRIMARY KEY,
    title    TEXT,            -- NULL: checked, no CORE match
    method   TEXT NOT NULL    -- title, acronym, words, unmatched, manual
);
