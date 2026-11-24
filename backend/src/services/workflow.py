import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, evidence_rows, runs
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time

#: A running attempt with no completed batch for this long is considered
#: interrupted and may be picked up again.  Reused batches keep the retry from
#: re-reading data that was already fully committed.
STALE_SECONDS = 600


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table, limit=100):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(limit)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-STALE_SECONDS)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return run.id, token, found(conn, cases, run.case_id)['payload']


def finish(run_id, token, result=None, error=None, store=None):
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        # The attempt token guard makes a retry idempotent: a stale worker
        # whose lease expired cannot overwrite the newer attempt, and the
        # unique run_id on evidence means no duplicate evidence row.
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return False
        if result is not None:
            digest = hashlib.sha256(canonical(result)).hexdigest()
            # A report is only ever written after every planned batch is in
            # hand, so its completeness is derivable from the stored report.
            evidence_id = conn.execute(evidence.insert().values(
                run_id=run_id, report=result, digest=digest, headline=headline(result),
            )).inserted_primary_key[0]
            _persist_rows(conn, evidence_id, result)
        log(conn, 'worker', status.value, run_id)
    if count and store is not None:
        store.release()
    return bool(count)


def headline(report):
    """Small index card so listing evidence never loads the matrix."""
    if not isinstance(report, dict):
        return {}
    return {
        'summary': report.get('summary'),
        'metrics': report.get('metrics') if isinstance(report.get('metrics'), dict) else {},
        'plan': report.get('plan'),
        'complete': domain.report_complete(report),
        'sections': [
            {'title': item.get('title'), 'count': len(item.get('rows') or [])}
            for item in report.get('sections', []) if isinstance(item, dict)
        ],
    }


def _persist_rows(conn, evidence_id, report):
    for item in report.get('sections', []):
        if not isinstance(item, dict):
            continue
        rows = item.get('rows') or []
        for start in range(0, len(rows), 1000):
            window = rows[start:start + 1000]
            conn.execute(evidence_rows.insert(), [
                {'evidence_id': evidence_id, 'section': item.get('title'),
                 'ordinal': start + offset, 'row': row}
                for offset, row in enumerate(window)
            ])


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        row = conn.execute(select(evidence.c.id, evidence.c.headline, evidence.c.reviewed_at)
                           .where(evidence.c.id == identity)).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该记录')
        card = row['headline'] if isinstance(row['headline'], dict) and row['headline'] else None
        if card is not None:
            complete = card.get('complete')
        else:
            # Pre-upgrade evidence: derive completeness from its report.
            report = conn.execute(select(evidence.c.report)
                                  .where(evidence.c.id == identity)).scalar_one()
            complete = domain.report_complete(report)
        if not complete:
            raise HTTPException(409, '证据未覆盖全部核对组合，暂不能签署。')
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
    return evidence_index(identity)


def evidence_index(identity):
    """Metadata for one stored evidence row, without shipping the rows."""
    with engine.connect() as conn:
        row = conn.execute(select(evidence.c.id, evidence.c.run_id, evidence.c.digest,
                                  evidence.c.headline, evidence.c.reviewer,
                                  evidence.c.review_note, evidence.c.reviewed_at)
                           .where(evidence.c.id == identity)).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该记录')
        row = dict(row)
        report = None
        if not row.get('headline'):
            # Pre-upgrade evidence has no index card; fall back to its report.
            report = conn.execute(select(evidence.c.report)
                                  .where(evidence.c.id == identity)).scalar_one()
        return _summary(row, report)


def evidence_summaries(limit=100):
    """List evidence as index cards; matrix rows are fetched page by page."""
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(
            select(evidence.c.id, evidence.c.run_id, evidence.c.digest,
                   evidence.c.headline, evidence.c.reviewer,
                   evidence.c.review_note, evidence.c.reviewed_at)
            .order_by(evidence.c.id.desc()).limit(limit)).mappings()]
        legacy = [row['id'] for row in rows if not row.get('headline')]
        reports = {}
        if legacy:
            # Only pre-upgrade rows lack a headline; load their reports once.
            for item in conn.execute(select(evidence.c.id, evidence.c.report)
                                     .where(evidence.c.id.in_(legacy))).mappings():
                reports[item['id']] = item['report']
        return [_summary(row, reports.get(row['id'])) for row in rows]


def _summary(row, report=None):
    # New rows carry a precomputed headline; older evidence falls back to the
    # full report so it stays readable.
    if report is None:
        report = row.get('report')
    card = row.get('headline')
    if not isinstance(card, dict) or not card:
        card = headline(report)
    report = report if isinstance(report, dict) else {}
    return {
        'id': row['id'], 'run_id': row['run_id'], 'digest': row['digest'],
        'reviewer': row['reviewer'], 'review_note': row['review_note'], 'reviewed_at': row['reviewed_at'],
        'summary': card.get('summary'),
        'metrics': card.get('metrics') or {},
        'complete': card.get('complete', domain.report_complete(report)),
        'plan': card.get('plan'),
        'sections': card.get('sections') or [],
    }
