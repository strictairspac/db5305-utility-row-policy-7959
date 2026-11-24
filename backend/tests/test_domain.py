"""Domain reconciliation without a live database.

The restricted read is replaced by an in-memory model of the RLS policy so the
batching, resume, completeness and determinism contracts can be checked
directly:

    PYTHONPATH=backend python3 -m pytest backend/tests/test_domain.py
"""

import copy

import pytest

from src.services import domain


def fake_read(engine, audit_at, operator_ids, orders, delegations):
    orders = list(orders)
    visible = {}
    for operator_id in operator_ids:
        seen = set()
        for order in orders:
            for delegation in delegations:
                if (delegation["operator_id"] == operator_id
                        and delegation["zone"] == order["zone"]
                        and delegation["utility"] == order["utility"]
                        and delegation["valid_from"] <= audit_at < delegation["valid_until"]):
                    seen.add(order["order_id"])
        visible[operator_id] = (f"role_{operator_id}", seen)
    return visible


@pytest.fixture(autouse=True)
def stub_reader(monkeypatch):
    monkeypatch.setattr(domain, "read_visible_orders", fake_read)


def sample():
    return copy.deepcopy(domain.SPEC["sample"])


def test_sample_matches_declared_expectations():
    result = domain.solve(sample(), engine=None)
    assert result["metrics"] == domain.SPEC["expect"]
    assert domain.report_complete(result)


def test_every_batch_is_bounded_and_reads_are_complete(monkeypatch):
    calls = []
    monkeypatch.setattr(domain, "read_visible_orders",
                        lambda *a: calls.append(len(list(a[3]))) or fake_read(*a))
    payload = sample()
    payload["work_orders"] = [{"order_id": f"O{i}", "meter_id": "M", "zone": "北闸", "utility": "水表"}
                              for i in range(1500)]
    payload["delegations"] = [{"operator_id": p["operator_id"], "zone": "北闸", "utility": "水表",
                               "valid_from": "2026-11-21T08:00:00+08:00",
                               "valid_until": "2026-11-21T12:00:00+08:00"} for p in payload["operators"]]
    payload["business_grants"] = [{"operator_id": p["operator_id"], "order_id": "O0"}
                                  for p in payload["operators"]]
    result = domain.solve(payload, engine=None)
    assert all(count <= 1000 for count in calls)
    assert result["metrics"]["核对组合数"] == 4 * 1500
    assert result["metrics"]["数据库可见数"] == 4 * 1500


class Recorder:
    def __init__(self, crash_after=None, seed=None):
        self.rows = dict(seed or {})
        self.crash_after = crash_after
        self.saves = 0
        self.reused = 0

    def load(self, fingerprint, plan_digest, batch_count):
        self.reused = len(self.rows)
        return copy.deepcopy(self.rows)

    def save(self, batch_index, fingerprint, plan_digest, payload):
        self.saves += 1
        self.rows[batch_index] = copy.deepcopy(payload)
        if self.crash_after is not None and self.saves == self.crash_after:
            raise RuntimeError("interrupted")


def big_payload():
    return {
        "audit_at": "2026-11-21T09:00:00+08:00",
        "operators": [{"operator_id": f"P{i}", "operator_name": "X"} for i in range(3)],
        "work_orders": [{"order_id": f"O{j}", "meter_id": "M", "zone": "北闸", "utility": "水表"}
                        for j in range(1500)],
        "delegations": [{"operator_id": f"P{i}", "zone": "北闸", "utility": "水表",
                         "valid_from": "2026-11-21T08:00:00+08:00",
                         "valid_until": "2026-11-21T12:00:00+08:00"} for i in range(3)],
        "business_grants": [{"operator_id": "P0", "order_id": "O0"}],
    }


def test_interruption_resumes_only_matching_batches(monkeypatch):
    calls = []
    monkeypatch.setattr(domain, "read_visible_orders", lambda *a: calls.append(1) or fake_read(*a))
    payload = big_payload()
    first = Recorder(crash_after=3)
    with pytest.raises(RuntimeError):
        domain.solve(payload, engine=None, store=first)
    assert len(calls) == 3

    calls.clear()
    second = Recorder(seed=first.rows)
    result = domain.solve(payload, engine=None, store=second)
    assert second.reused == 3
    assert len(calls) == 2  # only the two unplayed batches are read again
    assert result["metrics"]["核对组合数"] == 4500


def test_edited_input_recomputes_the_whole_plan():
    payload = big_payload()
    first = Recorder()
    domain.solve(payload, engine=None, store=first)
    edited = copy.deepcopy(payload)
    edited["work_orders"][0]["zone"] = "南堤"
    fresh = Recorder()
    domain.solve(edited, engine=None, store=fresh)
    assert fresh.reused == 0
    assert fresh.saves == 5


def test_corrupt_checkpoint_is_recomputed():
    payload = big_payload()
    corrupted = Recorder(seed={0: {"roles": {}, "visible": {}}})  # missing people
    calls = []
    original = domain.read_visible_orders

    def counting(*a):
        calls.append(1)
        return fake_read(*a)

    domain.read_visible_orders = counting
    try:
        result = domain.solve(payload, engine=None, store=corrupted)
    finally:
        domain.read_visible_orders = original
    assert len(calls) == 5  # the bad batch is redone, not trusted
    assert result["metrics"]["核对组合数"] == 4500


def test_digest_is_deterministic_for_the_same_input():
    import hashlib

    from src.utils.encoding import canonical

    one = domain.solve(sample(), engine=None)
    two = domain.solve(sample(), engine=None)
    assert hashlib.sha256(canonical(one)).hexdigest() == hashlib.sha256(canonical(two)).hexdigest()


def test_incomplete_report_cannot_be_signed():
    result = domain.solve(sample(), engine=None)
    result["metrics"]["核对组合数"] = 19
    assert not domain.report_complete(result)


def test_legacy_report_without_plan_is_still_complete():
    legacy = {
        "summary": "s",
        "metrics": {"核对组合数": 2},
        "sections": [{"title": "人员可见集", "rows": []},
                     {"title": "差异清单", "rows": []},
                     {"title": "逐工单访问矩阵", "rows": [{"a": 1}, {"a": 2}]}],
    }
    assert domain.report_complete(legacy)
