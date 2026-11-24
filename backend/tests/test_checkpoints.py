"""Checkpoint store and evidence idempotency against a throwaway SQLite engine.

SQLite only stands in for the durable tables here; the RLS replay itself is
PostgreSQL-specific and tested in ``test_domain.py`` with an in-memory policy.
"""

import pytest
from sqlalchemy import create_engine, func, select, update

from src.constants.workflow import Status
from src.models.tables import cases, evidence, run_batches, runs, schema
from src.services import checkpoints, workflow
from src.utils.time import utc_time


@pytest.fixture
def engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    schema.create_all(eng)
    # ``workflow`` binds ``engine`` at import time; the store imports it lazily.
    monkeypatch.setattr("src.config.settings.engine", eng)
    monkeypatch.setattr("src.services.workflow.engine", eng)
    return eng


def new_run(engine, token):
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(
            slug="s", title="t", payload={}, author="a", created_at=utc_time())).inserted_primary_key[0]
        return conn.execute(runs.insert().values(
            case_id=case_id, status=Status.RUNNING, owner="a",
            attempt_token=token, created_at=utc_time())).inserted_primary_key[0]


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_checkpoints_survive_and_renew_the_lease(engine):
    run_id = new_run(engine, "tok")
    store = checkpoints.Store(run_id, "tok")
    store.save(0, "fp", "pd", {"roles": {}, "visible": {}})
    store.save(1, "fp", "pd", {"roles": {}, "visible": {}})
    assert store.load("fp", "pd", 5) == {0: {"roles": {}, "visible": {}}, 1: {"roles": {}, "visible": {}}}
    assert store.reused == 2


def test_stale_fingerprint_is_dropped(engine):
    run_id = new_run(engine, "tok")
    store = checkpoints.Store(run_id, "tok")
    store.save(0, "old", "pd", {"roles": {}, "visible": {}})
    assert store.load("new", "pd", 1) == {}
    assert count(engine, run_batches) == 0


def test_a_taken_over_lease_fences_writes(engine):
    run_id = new_run(engine, "tok")
    store = checkpoints.Store(run_id, "tok")
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(attempt_token="newer"))
    with pytest.raises(checkpoints.LeaseLost):
        store.save(0, "fp", "pd", {"roles": {}, "visible": {}})


def test_finish_is_idempotent_and_releases_checkpoints(engine):
    run_id = new_run(engine, "tok")
    store = checkpoints.Store(run_id, "tok")
    store.save(0, "fp", "pd", {"roles": {}, "visible": {}})
    report = {"summary": "s", "metrics": {"核对组合数": 0}, "complete": True,
              "sections": [{"title": "人员可见集", "rows": []},
                           {"title": "差异清单", "rows": []},
                           {"title": "逐工单访问矩阵", "rows": []}]}
    assert workflow.finish(run_id, "tok", result=report, store=store) is True
    assert count(engine, evidence) == 1
    assert count(engine, run_batches) == 0
    # A stale retry (different token) is ignored and cannot add a second row.
    assert workflow.finish(run_id, "stale", result=report) is False
    assert count(engine, evidence) == 1
