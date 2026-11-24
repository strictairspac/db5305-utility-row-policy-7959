"""Pure batching contracts (no database required).

    PYTHONPATH=backend python3 -m pytest backend/tests/test_batching.py
"""

import itertools

import pytest

from src.services import batching


@pytest.mark.parametrize("people,orders", [(4, 5), (3, 1001), (1, 2000), (1001, 1), (2003, 1999)])
def test_every_pair_exactly_once_and_bounded(people, orders):
    plan = batching.build([f"P{i}" for i in range(people)], [f"O{i}" for i in range(orders)])
    seen = []
    for batch in plan.batches:
        assert batch.pair_count <= batching.MAX_PAIRS
        seen.extend(batching.pairs(plan, batch))
    expected = set(itertools.product((f"P{i}" for i in range(people)), (f"O{i}" for i in range(orders))))
    assert len(seen) == len(set(seen)) == people * orders
    assert set(seen) == expected


def test_empty_catalogue_still_reads_each_person():
    plan = batching.build([f"P{i}" for i in range(1400)], [])
    assert len(plan.batches) == 2
    assert [len(batch.operator_ids) for batch in plan.batches] == [1000, 400]
    assert sum(batch.pair_count for batch in plan.batches) == 0
    assert plan.pair_count == 0


def test_fingerprint_is_order_independent_but_content_sensitive():
    base = {"audit_at": "t", "operators": [{"operator_id": "P1"}], "work_orders": []}
    same = {"work_orders": [], "operators": [{"operator_id": "P1"}], "audit_at": "t"}
    assert batching.fingerprint(base) == batching.fingerprint(same)
    assert batching.fingerprint(base) != batching.fingerprint({**base, "audit_at": "t2"})


def test_plan_digest_tracks_batch_boundaries():
    operators = [f"P{i}" for i in range(4)]
    orders = [f"O{i}" for i in range(600)]
    assert batching.plan_digest(batching.build(operators, orders, 1000)) == \
        batching.plan_digest(batching.build(operators, orders, 1000))
    # A different batch size uses different boundaries, so checkpoints from the
    # old layout must not be reused for the new one.
    assert batching.plan_digest(batching.build(operators, orders, 1000)) != \
        batching.plan_digest(batching.build(operators, orders, 500))
