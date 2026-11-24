"""Bounded paging over stored evidence.

New evidence is persisted twice: the full report (for export and the digest)
and one indexed row per matrix entry.  Paging reads at most ``size`` rows from
``business_evidence_rows``, so looking up one page never loads the whole
matrix.  Evidence written before this split has no indexed rows and falls back
to slicing its stored report.

Every page is a view over the stored evidence; paging never rewrites it, so the
digest is identical for every page size.  Ordering by ``ordinal`` plus the
section filter makes the traversal gap-free and duplicate-free.
"""

from sqlalchemy import func, select

from src.models.tables import evidence_rows
from src.services.batching import MAX_PAIRS

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = MAX_PAIRS


def _sections(headline):
    sections = headline.get("sections") if isinstance(headline, dict) else None
    if not isinstance(sections, list):
        return []
    return [item for item in sections if isinstance(item, dict)]


def resolve_section(headline, section):
    sections = _sections(headline)
    names = [item.get("title") for item in sections]
    if section in names:
        return sections[names.index(section)]
    if isinstance(section, str) and section.isdecimal() and int(section) < len(sections):
        return sections[int(section)]
    raise ValueError("找不到该证据分区。")


def page_size(value, default=DEFAULT_PAGE_SIZE):
    try:
        size = DEFAULT_PAGE_SIZE if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        raise ValueError("每页行数须为整数。") from None
    if not 1 <= size <= MAX_PAGE_SIZE:
        raise ValueError(f"每页行数须在 1 到 {MAX_PAGE_SIZE} 之间。")
    return size


def page_offset(value):
    try:
        offset = 0 if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        raise ValueError("翻页偏移须为整数。") from None
    if offset < 0:
        raise ValueError("翻页偏移须为非负整数。")
    return offset


def paginate(conn, evidence_id, headline, section, size=None, offset=None, legacy_rows=None):
    """One page plus enough metadata to traverse every row exactly once.

    ``headline`` is the small index card (section titles and counts).  New
    evidence pages straight from ``business_evidence_rows``.  Evidence stored
    before indexed rows existed passes its section rows through
    ``legacy_rows`` and is sliced in memory.
    """
    item = resolve_section(headline, section)
    title = item.get("title")
    size = page_size(size)
    offset = page_offset(offset)

    if legacy_rows is not None:
        window = legacy_rows[offset:offset + size]
        total = len(legacy_rows)
    else:
        total = conn.execute(select(func.count()).select_from(evidence_rows).where(
            evidence_rows.c.evidence_id == evidence_id,
            evidence_rows.c.section == title)).scalar_one()
        window = list(conn.execute(select(evidence_rows.c.row)
                                   .where(evidence_rows.c.evidence_id == evidence_id,
                                          evidence_rows.c.section == title)
                                   .order_by(evidence_rows.c.ordinal)
                                   .offset(offset).limit(size)).scalars().all())

    return {
        "section": title,
        "rows": window,
        "offset": offset,
        "size": size,
        "total": total,
        "has_previous": offset > 0,
        "has_next": offset + len(window) < total,
        "complete": headline.get("complete") is not False,
        "summary": headline.get("summary"),
    }
