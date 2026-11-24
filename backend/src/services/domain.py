"""Reconcile dispatch-approved meter work orders with observed PostgreSQL access."""

from dataclasses import dataclass
from datetime import UTC, datetime

from . import batching
from .row_security import read_visible_orders


SPEC = {
    "name": "埠灯公服·抄表工单分域",
    "summary": "按人员核对派工确认的工单授权与数据库行级策略返回的工单，定位越权和漏放。",
    "customer": "埠灯公共服务有限公司 · 表务调度与内控组",
    "entity_labels": ["抄表工单", "权限验签", "差异清单"],
    "field_labels": {
        "audit_at": "验签时点（含时区）",
        "operators": "待验签人员",
        "operator_id": "人员编号",
        "operator_name": "姓名",
        "work_orders": "抄表工单",
        "order_id": "工单编号",
        "meter_id": "表计编号",
        "zone": "作业区域",
        "utility": "表计类型",
        "delegations": "数据库区域委派",
        "valid_from": "委派生效（含时区）",
        "valid_until": "委派到期（含时区）",
        "business_grants": "派工确认的人员工单授权",
    },
    "guidance": "录入同一批次的人员、工单和区域委派，再按复核表逐条填写人员工单授权。",
    "sample": {
        "audit_at": "2026-11-21T09:00:00+08:00",
        "operators": [
            {"operator_id": "BD-017", "operator_name": "林澄"},
            {"operator_id": "BD-026", "operator_name": "许遥"},
            {"operator_id": "BD-034", "operator_name": "乔榆"},
            {"operator_id": "BD-041", "operator_name": "程仪"},
        ],
        "work_orders": [
            {"order_id": "MR-701", "meter_id": "W-N018", "zone": "北闸", "utility": "水表"},
            {"order_id": "MR-702", "meter_id": "E-N048", "zone": "北闸", "utility": "电表"},
            {"order_id": "MR-703", "meter_id": "W-S022", "zone": "南堤", "utility": "水表"},
            {"order_id": "MR-704", "meter_id": "W-W011", "zone": "西港", "utility": "水表"},
            {"order_id": "MR-705", "meter_id": "E-S109", "zone": "南堤", "utility": "电表"},
        ],
        "delegations": [
            {
                "operator_id": "BD-017", "zone": "北闸", "utility": "水表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T12:00:00+08:00",
            },
            {
                "operator_id": "BD-017", "zone": "南堤", "utility": "水表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T10:00:00+08:00",
            },
            {
                "operator_id": "BD-026", "zone": "北闸", "utility": "电表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T12:00:00+08:00",
            },
            {
                "operator_id": "BD-026", "zone": "南堤", "utility": "水表",
                "valid_from": "2026-11-21T08:00:00+08:00", "valid_until": "2026-11-21T09:00:00+08:00",
            },
            {
                "operator_id": "BD-041", "zone": "西港", "utility": "水表",
                "valid_from": "2026-11-21T09:00:00+08:00", "valid_until": "2026-11-21T12:00:00+08:00",
            },
        ],
        "business_grants": [
            {"operator_id": "BD-017", "order_id": "MR-701"},
            {"operator_id": "BD-026", "order_id": "MR-702"},
            {"operator_id": "BD-026", "order_id": "MR-703"},
            {"operator_id": "BD-041", "order_id": "MR-704"},
        ],
    },
    "expect": {
        "验签人员数": 4, "核对组合数": 20, "业务授权数": 4, "数据库可见数": 4,
        "越权可见数": 1, "应见未见数": 1, "差异数": 2,
    },
}

SECTION_PEOPLE = "人员可见集"
SECTION_DIFFERENCES = "差异清单"
SECTION_MATRIX = "逐工单访问矩阵"


@dataclass(frozen=True)
class Context:
    audit_at: datetime
    operators: dict
    orders: dict
    delegations: list
    expected: dict
    normalized: dict


def _records(payload, field, *columns):
    records = payload.get(field)
    if not isinstance(records, list):
        raise ValueError(f"{SPEC['field_labels'][field]}须为数组。")
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"{SPEC['field_labels'][field]}第 {index} 行须为对象。")
        for column in columns:
            value = record.get(column)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{SPEC['field_labels'][field]}第 {index} 行的{SPEC['field_labels'][column]}须为非空文本。")
    return records


def _time(value, label):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is not None:
            return timestamp
    except (TypeError, ValueError):
        pass
    raise ValueError(f"{label}须使用含时区的 ISO 8601 日期时间。")


def _index(records, key):
    index = {}
    for record in records:
        value = record[key]
        if value in index:
            raise ValueError(f"{SPEC['field_labels'][key]}重复（{value}）。")
        index[value] = record
    return index


def read_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("验签输入须为对象。")
    audit_at = _time(payload.get("audit_at"), "验签时点")
    operators = _index(_records(payload, "operators", "operator_id", "operator_name"), "operator_id")
    orders = _index(_records(payload, "work_orders", "order_id", "meter_id", "zone", "utility"), "order_id")
    if not operators:
        raise ValueError("至少需要一名待验签人员。")
    delegations = []
    for row in _records(payload, "delegations", "operator_id", "zone", "utility", "valid_from", "valid_until"):
        if row["operator_id"] not in operators:
            raise ValueError(f"区域委派引用了未知人员（{row['operator_id']}）。")
        start = _time(row["valid_from"], "委派生效时间")
        end = _time(row["valid_until"], "委派到期时间")
        if start >= end:
            raise ValueError(f"人员 {row['operator_id']} 的委派到期时间须晚于生效时间。")
        delegations.append({**row, "valid_from": start, "valid_until": end})

    for row in (*orders.values(), *delegations):
        if row["utility"] not in {"水表", "电表"}:
            raise ValueError("表计类型须为水表或电表。")

    expected = {operator_id: set() for operator_id in operators}
    for row in _records(payload, "business_grants", "operator_id", "order_id"):
        operator_id, order_id = row["operator_id"], row["order_id"]
        if operator_id not in operators or order_id not in orders:
            raise ValueError(f"业务授权引用了未知人员或工单（{operator_id} / {order_id}）。")
        expected[operator_id].add(order_id)

    normalized = _normalize(audit_at, operators, orders, delegations, expected)
    return Context(audit_at, operators, orders, delegations, expected, normalized)


def _normalize(audit_at, operators, orders, delegations, expected):
    """Order-independent semantic form; any change invalidates stored batches."""
    people = [{"operator_id": key, "operator_name": value["operator_name"]}
              for key, value in sorted(operators.items())]
    work_orders = [{key: value[key] for key in ("order_id", "meter_id", "zone", "utility")}
                   for _, value in sorted(orders.items())]
    windows = sorted({
        (row["operator_id"], row["zone"], row["utility"],
         row["valid_from"].astimezone(UTC).isoformat(), row["valid_until"].astimezone(UTC).isoformat())
        for row in delegations
    })
    grants = sorted((operator_id, order_id)
                    for operator_id, order_ids in expected.items() for order_id in order_ids)
    return {
        "audit_at": audit_at.astimezone(UTC).isoformat(),
        "operators": people,
        "work_orders": work_orders,
        "delegations": [dict(zip(("operator_id", "zone", "utility", "valid_from", "valid_until"), row))
                        for row in windows],
        "business_grants": [{"operator_id": operator_id, "order_id": order_id}
                            for operator_id, order_id in grants],
    }


def validate(payload):
    read_payload(payload)


def _read_batch(context, batch, engine):
    """Replay the restricted read for one bounded slice of pairs."""
    batch_orders = [context.orders[order_id] for order_id in batch.order_ids]
    batch_delegations = [row for row in context.delegations if row["operator_id"] in batch.operator_ids]
    readings = read_visible_orders(
        engine, context.audit_at, list(batch.operator_ids), batch_orders, batch_delegations)
    known = set(context.orders)
    return {
        "roles": {operator_id: role for operator_id, (role, _) in readings.items()},
        # A batch may only load a subset of the catalogue; keep known ids only.
        "visible": {operator_id: sorted(seen & known)
                    for operator_id, (_, seen) in readings.items()},
    }


def _merge(readings, batch, payload):
    for operator_id in batch.operator_ids:
        entry = readings.setdefault(operator_id, {"role": payload["roles"][operator_id], "visible": set()})
        entry["visible"].update(payload["visible"].get(operator_id, ()))


def _usable(payload, batch):
    """A checkpoint is reusable only if it covers this batch's people."""
    if not isinstance(payload, dict):
        return False
    roles = payload.get("roles")
    visible = payload.get("visible")
    if not isinstance(roles, dict) or not isinstance(visible, dict):
        return False
    return all(operator_id in roles and isinstance(visible.get(operator_id), list)
               for operator_id in batch.operator_ids)


def _read_batches(context, engine, store):
    plan = batching.build(context.operators, context.orders)
    fingerprint = batching.fingerprint(context.normalized)
    plan_digest = batching.plan_digest(plan)
    stored = {}
    if store is not None:
        stored = store.load(fingerprint, plan_digest, len(plan.batches))
    readings = {}
    for batch in plan.batches:
        payload = stored.get(batch.index)
        if not _usable(payload, batch):
            # Missing, mismatched or corrupt checkpoint: recompute this batch.
            payload = _read_batch(context, batch, engine)
            if store is not None:
                store.save(batch.index, fingerprint, plan_digest, payload)
        _merge(readings, batch, payload)
    return plan, fingerprint, readings


def _build_rows(context, plan, readings):
    people, matrix, differences = [], [], []
    excess_count = missing_count = 0
    for operator_id in plan.operators:
        observed = readings[operator_id]
        person = {"人员编号": operator_id, "姓名": context.operators[operator_id]["operator_name"]}
        approved = context.expected[operator_id]
        visible = observed["visible"]
        excess, missing = visible - approved, approved - visible
        excess_count += len(excess)
        missing_count += len(missing)
        people.append({
            **person, "数据库角色": observed["role"], "工单与委派 RLS 生效": "是",
            "业务授权工单": "、".join(sorted(approved)), "数据库可见工单": "、".join(sorted(visible)),
            "越权可见数": len(excess), "应见未见数": len(missing),
        })
        for order_id in plan.orders:
            order = context.orders[order_id]
            if order_id in excess:
                verdict = "越权可见"
            elif order_id in missing:
                verdict = "应见未见"
            elif order_id in visible:
                verdict = "授权可见"
            else:
                verdict = "正确拦截"
            row = {
                **person, "工单编号": order_id, "表计编号": order["meter_id"],
                "作业区域": order["zone"], "表计类型": order["utility"],
                "业务授权": "是" if order_id in approved else "否",
                "数据库可见": "是" if order_id in visible else "否", "判定": verdict,
            }
            matrix.append(row)
            if order_id in excess or order_id in missing:
                differences.append(row)
    return people, matrix, differences, excess_count, missing_count


def solve(payload, engine, store=None):
    context = read_payload(payload)
    plan, fingerprint, readings = _read_batches(context, engine, store)
    people, matrix, differences, excess_count, missing_count = _build_rows(context, plan, readings)

    if differences:
        summary = f"验签发现 {excess_count} 组越权可见和 {missing_count} 组应见未见，请复核区域委派。"
    elif not plan.orders:
        summary = "本批没有抄表工单，已用受限角色验证空可见集。"
    else:
        summary = f"验签通过，{len(plan.operators)} 名人员的数据库可见集与派工授权一致。"
    return {
        "summary": summary,
        "metrics": {
            "验签人员数": len(plan.operators), "核对组合数": len(matrix),
            "业务授权数": sum(map(len, context.expected.values())),
            "数据库可见数": sum(len(entry["visible"]) for entry in readings.values()),
            "越权可见数": excess_count, "应见未见数": missing_count, "差异数": len(differences),
        },
        "complete": True,
        "plan": {
            "version": batching.PLAN_VERSION, "max_pairs": batching.MAX_PAIRS,
            "batch_count": len(plan.batches), "fingerprint": fingerprint,
        },
        "sections": [
            {"title": SECTION_PEOPLE, "rows": people},
            {"title": SECTION_DIFFERENCES, "rows": differences},
            {"title": SECTION_MATRIX, "rows": matrix},
        ],
    }


def report_complete(report):
    """Signing is only allowed once every planned pair is present."""
    if not isinstance(report, dict):
        return False
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    sections = report.get("sections") if isinstance(report.get("sections"), list) else []
    matrix = next((item.get("rows") for item in sections
                   if isinstance(item, dict) and item.get("title") == SECTION_MATRIX), None)
    if not isinstance(matrix, list):
        return False
    if metrics.get("核对组合数") != len(matrix):
        return False
    plan = report.get("plan")
    if plan is None:
        # Evidence produced before batching; its matrix was written in one go.
        return True
    if plan.get("version") != batching.PLAN_VERSION:
        return False
    if report.get("complete") is not True:
        return False
    batch_count = plan.get("batch_count")
    max_pairs = plan.get("max_pairs", batching.MAX_PAIRS)
    return isinstance(batch_count, int) and batch_count >= 0 and isinstance(max_pairs, int) and 0 < max_pairs <= batching.MAX_PAIRS
