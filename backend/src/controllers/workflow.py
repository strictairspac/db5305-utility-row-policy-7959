from fastapi import HTTPException
from sqlalchemy import select
from starlette.responses import Response

from src.config.settings import engine
from src.models.tables import evidence, evidence_rows
from src.services import paging
from src.services.workflow import headline
from src.utils.encoding import canonical

META_COLUMNS = ('id', 'run_id', 'digest', 'reviewer', 'review_note', 'reviewed_at')


def download(identity):
    with engine.connect() as conn:
        data = conn.execute(select(evidence).where(evidence.c.id == identity)).mappings().first()
    if data is None:
        raise HTTPException(404, '找不到该记录')
    # The export is the persisted report, not a page: paging never changes it.
    return Response(canonical(dict(data)), media_type='application/json', headers={
        'Content-Disposition': f'attachment; filename="evidence-{identity}.json"'})


def page(identity, section, size, offset):
    with engine.connect() as conn:
        row = conn.execute(select(
            evidence.c.id, evidence.c.run_id, evidence.c.digest, evidence.c.headline,
            evidence.c.reviewer, evidence.c.review_note, evidence.c.reviewed_at,
        ).where(evidence.c.id == identity)).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该记录')
        card = row['headline'] if isinstance(row['headline'], dict) and row['headline'] else None
        indexed = conn.execute(select(evidence_rows.c.id)
                               .where(evidence_rows.c.evidence_id == identity)
                               .limit(1)).first() is not None
        report = None
        if card is None or not indexed:
            # Only pre-upgrade evidence needs its report; new evidence is
            # served entirely from the index card and row table.
            report = conn.execute(select(evidence.c.report)
                                  .where(evidence.c.id == identity)).scalar_one()
            card = card or headline(report)
        legacy_rows = None if indexed else _section_rows(report, card, section)
        try:
            result = paging.paginate(conn, identity, card, section, size, offset, legacy_rows)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    result.update({key: row[key] for key in META_COLUMNS})
    return result


def _section_rows(report, card, section):
    try:
        title = paging.resolve_section(card, section).get('title')
    except ValueError:
        return None  # paginate reports the bad section itself
    for item in report.get('sections') or []:
        if isinstance(item, dict) and item.get('title') == title:
            rows = item.get('rows')
            return rows if isinstance(rows, list) else []
    return []
