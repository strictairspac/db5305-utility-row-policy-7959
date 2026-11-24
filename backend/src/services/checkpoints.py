"""Durable per-batch checkpoints for resumable verification.

A batch row is written in its own transaction as soon as that slice of the
matrix has been read.  If the worker dies, the next attempt reuses every
checkpoint whose fingerprint still matches the current input.  A checkpoint
saved under a different fingerprint (any input edited, plan version bumped,
batch size or boundaries changed) is never reused; the whole plan is
recomputed.

Which rows survive an interruption, precisely:

* a batch is reusable only when ``fingerprint``, ``plan_digest`` and
  ``batch_index`` all equal the freshly planned values;
* changing any semantic input (people, work orders, delegations, business
  grants, audit time) changes the fingerprint, so **every** batch is redone;
* changing the plan shape (plan version or batch size) changes the plan
  digest, so **every** batch is redone even when the input is unchanged;
* a completed run keeps its evidence and releases its checkpoints.

Each save also renews the run lease (``started_at``) under the attempt token.
If another worker has taken the run over, the token no longer matches and
:class:`LeaseLost` aborts this attempt instead of mixing two writers' rows.

Writing evidence is idempotent: the unique ``run_id`` on ``business_evidence``
plus the attempt token on the run means a retry cannot produce a second
evidence row, and a stale attempt cannot overwrite a newer one.
"""

from sqlalchemy import delete, or_, select

from src.constants.workflow import Status
from src.models.tables import run_batches, runs
from src.utils.time import utc_time


class LeaseLost(RuntimeError):
    """Another attempt owns the run; stop writing and leave its rows alone."""


class Store:
    def __init__(self, run_id, token):
        self.run_id = run_id
        self.token = token
        self.reused = 0
        self.computed = 0

    def load(self, fingerprint, plan_digest, batch_count):
        """Return reusable checkpoints keyed by batch index."""
        with _engine().begin() as conn:
            # Drop anything from an older plan (edited input, changed batching)
            # so a later retry cannot mistake it for a valid checkpoint.
            conn.execute(delete(run_batches).where(
                run_batches.c.run_id == self.run_id,
                or_(run_batches.c.fingerprint != fingerprint,
                    run_batches.c.plan_digest != plan_digest,
                    run_batches.c.batch_index >= batch_count)))
            rows = conn.execute(select(run_batches.c.batch_index, run_batches.c.payload)
                                .where(run_batches.c.run_id == self.run_id,
                                       run_batches.c.fingerprint == fingerprint,
                                       run_batches.c.plan_digest == plan_digest)).all()
        found = {row.batch_index: row.payload for row in rows}
        self.reused = len(found)
        return found

    def save(self, batch_index, fingerprint, plan_digest, payload):
        """Persist one completed batch and renew the lease in one transaction."""
        with _engine().begin() as conn:
            owned = conn.execute(runs.update().where(
                runs.c.id == self.run_id, runs.c.attempt_token == self.token,
                runs.c.status == Status.RUNNING).values(started_at=utc_time())).rowcount
            if not owned:
                raise LeaseLost(f"运行 {self.run_id} 已由新的尝试接管。")
            conn.execute(delete(run_batches).where(run_batches.c.run_id == self.run_id,
                                                   run_batches.c.batch_index == batch_index))
            conn.execute(run_batches.insert().values(
                run_id=self.run_id, batch_index=batch_index, fingerprint=fingerprint,
                plan_digest=plan_digest, payload=payload, created_at=utc_time()))
        self.computed += 1

    def release(self):
        """Free checkpoints once the final evidence has been persisted."""
        clear(self.run_id)


def _engine():
    # Imported lazily so the pure batching/domain modules stay importable
    # without database settings.
    from src.config.settings import engine
    return engine


def clear(run_id):
    with _engine().begin() as conn:
        conn.execute(delete(run_batches).where(run_batches.c.run_id == run_id))
