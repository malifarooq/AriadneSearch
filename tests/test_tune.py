import json

import pytest

from conftest import FakeOpenAlex, make_client, work
from ariadnesearch.errors import BadInput
from ariadnesearch.expand import add_seeds
from ariadnesearch.tune import GRID, tune_project


def a_field(seeds: int) -> list[dict]:
    """Seed papers that cite shared foundations and each other, and are cited together."""
    works = [work(100 + i, f"Foundation {i}", 2005, (), 500) for i in range(4)]
    for i in range(seeds):
        refs = (100 + i % 4, 101, 1 + (i + 1) % seeds)
        works.append(work(1 + i, f"Seed paper {i}", 2015 + i % 8, refs, 20, f"10.1000/seed.{i}"))
    for j in range(6):
        cited = tuple(1 + (j * 3 + k) % seeds for k in range(5))
        works.append(work(300 + j, f"Citing survey {j}", 2024, cited, 3, f"10.1000/citer.{j}"))
    return works


def test_tuning_needs_enough_seeds(conn, client, project):
    add_seeds(conn, client, project, ["10.1109/seed.1", "10.1145/seed.2"])
    with pytest.raises(BadInput, match="at least"):
        tune_project(conn, client, project)


def test_tuning_saves_weights_on_the_project_only(conn, project):
    fake = FakeOpenAlex(a_field(20))
    client = make_client(conn, fake)
    add_seeds(conn, client, project, [f"10.1000/seed.{i}" for i in range(20)])
    best, result = tune_project(conn, client, project, splits=2)

    assert set(best) == set(GRID)
    saved = json.loads(conn.execute("SELECT weights_json FROM project WHERE id = ?", (project,)).fetchone()[0])
    assert {key: saved[key] for key in GRID} == best
    assert result.recall.shape[1] == 2
    assert [row[0] for row in conn.execute("SELECT id FROM project")] == [project]  # splits cleaned up
    assert conn.execute("SELECT COUNT(*) FROM event WHERE type = 'tune'").fetchone()[0] == 1
