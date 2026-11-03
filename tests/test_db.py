import sqlite3
from pathlib import Path

import pytest

from ariadnesearch.db import SCHEMA_VERSION, cache_path, init_db
from ariadnesearch.errors import BadInput

V1_SCHEMA = (Path(__file__).parent / "schema_v1.sql").read_text(encoding="utf-8")


def make_v1(path: Path) -> None:
    """A small database in the version 1 layout, with text work IDs."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(V1_SCHEMA)
    conn.executescript(
        """
        INSERT INTO work(id, title, level, n_refs) VALUES
            ('wseed', 'A seed', 'hydrated', 2), ('wref', NULL, 'stub', 0), ('wnew', 'Filled', 'hydrated', 0);
        INSERT INTO work_alias VALUES ('openalex', 'W1', 'wseed'), ('doi', '10.1000/a', 'wseed'), ('openalex', 'W2', 'wref');
        INSERT INTO cites VALUES ('wseed', 'wref', 'openalex', '2026-01-01T00:00:00+00:00'),
                                 ('wnew', 'wref', 'arxiv', '2026-01-02T00:00:00+00:00');
        INSERT INTO project(id, name) VALUES (1, 'kept');
        INSERT INTO membership VALUES (1, 'wseed', 'seed', 1.5, 2.5, '2026-01-01T00:00:00+00:00');
        INSERT INTO my_paper(id, title, status) VALUES (1, 'Draft', 'draft');
        INSERT INTO my_citation VALUES (1, 'wref', 'aux');
        INSERT INTO merge_log(keep_id, drop_id, rule, confidence, snapshot_json, ts)
            VALUES ('wseed', 'wgone', 'shared-id', 1.0, '{}', '2026-01-01');
        INSERT INTO raw_response VALUES
            ('k1', '/works/W1?select=id', x'00', '2026-01-01T00:00:00+00:00'),
            ('k2', '/works?cursor=abc&filter=cites:W1', x'00', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()


def test_a_new_database_uses_the_current_layout(tmp_path):
    conn = init_db(tmp_path / "graph.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM cache.raw_response").fetchone()[0] == 0
    assert cache_path(tmp_path / "graph.db").exists()
    conn.close()


def test_a_version_1_database_is_upgraded_and_kept_as_a_backup(tmp_path):
    path = tmp_path / "graph.db"
    make_v1(path)
    conn = init_db(path)

    ids = dict(conn.execute("SELECT value, work_id FROM work_alias WHERE scheme = 'openalex'").fetchall())
    seed, ref = ids["W1"], ids["W2"]
    assert isinstance(seed, int) and isinstance(ref, int)
    links = {(row[0], row[1]): row[2] for row in conn.execute("SELECT src, dst, source FROM cites")}
    assert links[(seed, ref)] is None  # an OpenAlex link
    assert "arxiv" in links.values()
    member = conn.execute("SELECT work_id, state, x, y FROM membership").fetchone()
    assert tuple(member) == (seed, "seed", 1.5, 2.5)
    assert conn.execute("SELECT work_id FROM my_citation").fetchone()[0] == ref
    assert conn.execute("SELECT n_refs FROM work WHERE id = ?", (seed,)).fetchone()[0] == 2
    assert [row[0] for row in conn.execute("SELECT key FROM cache.raw_response")] == ["k1"]
    assert conn.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0] == 0
    conn.close()

    backup = tmp_path / "graph.v1-backup.db"
    old = sqlite3.connect(backup)
    assert old.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0] == 1
    old.close()
    init_db(path).close()  # a second start changes nothing
    assert not (tmp_path / "graph.v1-backup-1.db").exists()


def test_the_upgrade_waits_for_other_processes(tmp_path):
    path = tmp_path / "graph.db"
    make_v1(path)
    other = sqlite3.connect(path)
    other.execute("BEGIN")
    other.execute("SELECT COUNT(*) FROM work").fetchone()
    writer = sqlite3.connect(path)
    writer.execute("INSERT INTO budget(source, day) VALUES ('openalex', '2026-01-01')")
    writer.commit()
    writer.close()
    with pytest.raises(BadInput, match="another"):
        init_db(path)
    other.close()
