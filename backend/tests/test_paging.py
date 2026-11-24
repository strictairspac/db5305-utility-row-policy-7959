"""Pure paging contracts (no database required).

    PYTHONPATH=backend python3 -m pytest backend/tests/test_paging.py
"""

import pytest

from src.services import paging

CARD = {
    "summary": "s",
    "complete": True,
    "sections": [
        {"title": "人员可见集", "count": 0},
        {"title": "差异清单", "count": 0},
        {"title": "逐工单访问矩阵", "count": 2345},
    ],
}


def legacy(rows):
    return rows


@pytest.mark.parametrize("size", [1, 7, 100, 1000])
def test_legacy_paging_reaches_every_row_once(size):
    rows = [{"n": i} for i in range(2345)]
    collected, offset = [], 0
    while True:
        page = paging.paginate(None, 1, CARD, "逐工单访问矩阵", size, offset, legacy(rows))
        collected.extend(page["rows"])
        if not page["has_next"]:
            break
        offset += len(page["rows"])
    assert [row["n"] for row in collected] == list(range(2345))


def test_page_size_capped_at_one_thousand():
    with pytest.raises(ValueError):
        paging.paginate(None, 1, CARD, "逐工单访问矩阵", 1001, 0, [])


def test_unknown_section_rejected():
    with pytest.raises(ValueError):
        paging.paginate(None, 1, CARD, "missing", 100, 0, [])


def test_offset_past_end_has_no_next():
    page = paging.paginate(None, 1, CARD, "逐工单访问矩阵", 100, 2345, list(range(2345)))
    assert page["rows"] == []
    assert page["total"] == 2345
    assert page["has_next"] is False


def test_section_selectable_by_index():
    assert paging.resolve_section(CARD, "1")["title"] == "差异清单"
