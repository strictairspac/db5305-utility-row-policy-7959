"""Deterministic, memory-bounded batching of person × work-order pairs.

The access matrix is row-major: every person is expanded over every work
order.  A batch never contains more than ``MAX_PAIRS`` pairs, so both the
database replay and the evidence paging work on at most one thousand
combinations at a time.
"""

from dataclasses import dataclass
from hashlib import sha256

from src.utils.encoding import canonical

#: Upper bound for one batch of verification work and one evidence page.
MAX_PAIRS = 1000

#: Bump when batch boundaries or the row layout change.  Stored checkpoints
#: from another plan version are discarded instead of being reused.
PLAN_VERSION = 1


@dataclass(frozen=True)
class Batch:
    """A contiguous slice of the row-major pair sequence."""

    index: int
    operator_ids: tuple[str, ...]
    order_ids: tuple[str, ...]
    first_pair: int
    pair_count: int


@dataclass(frozen=True)
class Plan:
    operators: tuple[str, ...]
    orders: tuple[str, ...]
    batches: tuple[Batch, ...]

    @property
    def pair_count(self) -> int:
        return len(self.operators) * len(self.orders)


def build(operator_ids, order_ids, max_pairs=MAX_PAIRS):
    """Plan pair batches without materialising the full matrix."""
    if max_pairs < 1:
        raise ValueError('批大小须为正整数。')
    operators = tuple(sorted(operator_ids))
    orders = tuple(sorted(order_ids))
    width = len(orders)
    total = len(operators) * width
    batches = []
    if total:
        for first in range(0, total, max_pairs):
            last = min(first + max_pairs, total)
            # dict.fromkeys keeps the deterministic row-major order while
            # de-duplicating a boundary batch that straddles two people.
            operator_slice = tuple(dict.fromkeys(operators[p // width] for p in range(first, last)))
            order_slice = tuple(dict.fromkeys(orders[p % width] for p in range(first, last)))
            batches.append(Batch(len(batches), operator_slice, order_slice, first, last - first))
    elif operators:
        # No work orders: still replay the restricted read for every person
        # so an empty visible set is proven, one bounded group at a time.
        for first in range(0, len(operators), max_pairs):
            chunk = operators[first:first + max_pairs]
            batches.append(Batch(len(batches), chunk, (), 0, 0))
    return Plan(operators, orders, tuple(batches))


def pairs(plan, batch):
    """Yield ``(operator_id, order_id)`` for a batch in canonical order."""
    width = len(plan.orders)
    if not width:
        return
    for position in range(batch.first_pair, batch.first_pair + batch.pair_count):
        yield plan.operators[position // width], plan.orders[position % width]


def fingerprint(normalized):
    """Hash the semantic input plus the batching contract."""
    return sha256(canonical({
        'plan_version': PLAN_VERSION,
        'max_pairs': MAX_PAIRS,
        'input': normalized,
    })).hexdigest()


def plan_digest(plan):
    """Hash the exact batch boundaries so checkpoints match one layout only."""
    return sha256(canonical({
        'plan_version': PLAN_VERSION,
        'operators': list(plan.operators),
        'orders': list(plan.orders),
        'batches': [[b.index, list(b.operator_ids), list(b.order_ids), b.first_pair, b.pair_count]
                    for b in plan.batches],
    })).hexdigest()
