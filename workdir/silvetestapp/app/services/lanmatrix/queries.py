"""Query helpers: server-side pagination, whitelist sort/filter (FR-SEARCH/§10.2).

Sort fields and filter operators are validated against whitelists so no
user-supplied string is ever concatenated into SQL (all via ORM binding).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import and_, or_

from ...models import TestItemRow

# System columns that may be sorted / filtered directly.
SORTABLE = frozenset({
    "case_id", "title", "module", "result", "priority", "workflow_status",
    "row_order", "updated_at", "created_at", "version",
})

FILTER_OPS = frozenset({
    "eq", "ne", "contains", "not_contains", "empty", "not_empty",
    "gt", "lt", "gte", "lte", "in",
})

_COLUMN = {
    "case_id": TestItemRow.case_id,
    "title": TestItemRow.title,
    "module": TestItemRow.module,
    "result": TestItemRow.result,
    "priority": TestItemRow.priority,
    "workflow_status": TestItemRow.workflow_status,
    "row_order": TestItemRow.row_order,
    "updated_at": TestItemRow.updated_at,
    "created_at": TestItemRow.created_at,
    "version": TestItemRow.version,
}


class QueryError(ValueError):
    """Raised for an invalid sort field or filter operator."""


def parse_sort(sort: Optional[str]) -> list[tuple[str, bool]]:
    """Parse ``"case_id:asc,updated_at:desc"`` -> [(field, asc_bool), ...]."""
    out: list[tuple[str, bool]] = []
    if not sort:
        return [("row_order", True)]
    for token in sort.split(","):
        token = token.strip()
        if not token:
            continue
        field, _, direction = token.partition(":")
        field = field.strip()
        if field not in SORTABLE:
            raise QueryError(f"invalid sort field: {field}")
        out.append((field, direction.strip().lower() != "desc"))
    return out or [("row_order", True)]


def apply_sort(query, sort: Optional[str]):
    for field, asc in parse_sort(sort):
        col = _COLUMN[field]
        query = query.order_by(col.asc() if asc else col.desc())
    return query


def build_filter_clause(conditions: list[dict[str, Any]], combinator: str = "and"):
    """Build a SQLAlchemy clause from a list of whitelist-checked conditions.

    Each condition: ``{"field": str, "op": str, "value": Any}``. Only system
    columns are directly filterable; custom fields are matched by callers via a
    JSON contains on ``custom_values`` (kept simple/portable here).
    """
    clauses = []
    for cond in conditions or []:
        field = cond.get("field")
        op = cond.get("op")
        value = cond.get("value")
        if op not in FILTER_OPS:
            raise QueryError(f"invalid operator: {op}")
        if field not in _COLUMN:
            raise QueryError(f"invalid filter field: {field}")
        col = _COLUMN[field]
        clauses.append(_op_clause(col, op, value))
    if not clauses:
        return None
    return or_(*clauses) if combinator == "or" else and_(*clauses)


def _op_clause(col, op: str, value: Any):
    if op == "eq":
        return col == value
    if op == "ne":
        return col != value
    if op == "contains":
        return col.ilike(f"%{value}%")
    if op == "not_contains":
        return ~col.ilike(f"%{value}%")
    if op == "empty":
        return or_(col.is_(None), col == "")
    if op == "not_empty":
        return and_(col.isnot(None), col != "")
    if op == "gt":
        return col > value
    if op == "lt":
        return col < value
    if op == "gte":
        return col >= value
    if op == "lte":
        return col <= value
    if op == "in":
        return col.in_(value if isinstance(value, (list, tuple)) else [value])
    raise QueryError(f"unhandled operator: {op}")
