"""Compare requested execution inputs with committed matrix rows."""

from __future__ import annotations

import json
import math
from typing import Any, Iterable

from ...extensions import db
from ...models import TestItemRow
from .. import run_evidence_service as evidence
from . import silver_json_export as export
from ...collab.writeback import lock_project


class RunInputsNotMaterialized(Exception):
    """The caller edited inputs that are not yet committed."""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        self.conflicts = conflicts
        super().__init__("Collaborative inputs are not materialized")


_MAX_SAFE_JS_INT = (1 << 53) - 1


def _normalize_json_numbers(value: Any) -> Any:
    """Align integral CRDT floats (``1.0`` from pycrdt) with JS integers (``1``)."""
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) <= _MAX_SAFE_JS_INT:
            return int(value)
        return value
    if isinstance(value, list):
        return [_normalize_json_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_json_numbers(val) for key, val in value.items()}
    return value


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return text
        if not isinstance(parsed, (dict, list)):
            return text
        value = parsed
    value = _normalize_json_numbers(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value).strip()


def _rows(project_id: int, sheet: str) -> list[TestItemRow]:
    return (TestItemRow.query
            .filter(TestItemRow.project_id == project_id,
                    TestItemRow.sheet == sheet,
                    TestItemRow.deleted_at.is_(None))
            .order_by(TestItemRow.row_order.asc(), TestItemRow.id.asc())
            .populate_existing()
            .all())


def _lib_name(row: TestItemRow) -> str:
    return str(row.get_field(export.LIB_FUNC) or row.get_field(export.LIB_NAME) or row.case_id or "").strip()


def _wanted_test_id_set(test_ids: Iterable[str] | None) -> set[str] | None:
    if test_ids is None:
        return None
    return {str(tid).strip() for tid in test_ids if str(tid).strip()}


def _current(project_id: int, *, wanted_test_ids: set[str] | None = None) -> dict[str, list[dict[str, str]]]:
    test_rows = _rows(project_id, "test")
    if wanted_test_ids is not None:
        test_rows = [row for row in test_rows if export.row_test_id(row) in wanted_test_ids]
    return {
        "tests": [{
            "uuid": row.uuid or "",
            "test_id": export.row_test_id(row),
            "steps": _canonical(row.get_field(export.TEST_STEPS)),
        } for row in test_rows],
        "lib": [{
            "uuid": row.uuid or "",
            "name": _lib_name(row),
            "steps": _canonical(row.get_field(export.LIB_STEPS)),
            "params": _canonical(row.get_field(export.LIB_PARA)),
            "init": _canonical(row.get_field(export.LIB_ISINIT)),
        } for row in _rows(project_id, "lib")],
        "const": [{
            "uuid": row.uuid or "",
            "name": _canonical(row.get_field(export.CONST_NAME)),
            "value": _canonical(row.get_field(export.CONST_VALUE)),
        } for row in _rows(project_id, "const")],
    }


def _expected_rows(expected: dict[str, Any], key: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
    rows = expected.get(key) or []
    if not isinstance(rows, list):
        raise evidence.EvidenceError("Execution input snapshot is invalid")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise evidence.EvidenceError("Execution input snapshot is invalid")
        normalized.append({field: _canonical(row.get(field)) for field in fields})
    return normalized


def _test_conflicts(wanted_rows: list[dict[str, str]],
                    current_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    sort_key = lambda row: (row["uuid"], row["test_id"], row["steps"])
    if sorted(wanted_rows, key=sort_key) == sorted(current_rows, key=sort_key):
        return []
    remaining = list(current_rows)
    conflicts: list[dict[str, str]] = []
    for row in wanted_rows:
        if row in remaining:
            remaining.remove(row)
        else:
            conflicts.append({"sheet": "test", "uuid": row["uuid"]})
    for row in remaining:
        conflicts.append({"sheet": "test", "uuid": row["uuid"]})
    return conflicts


def assert_materialized(
    project_id: int,
    expected: dict[str, Any],
    *,
    test_ids: Iterable[str] | None = None,
) -> None:
    """Lock the project and reject inputs that differ from the database."""
    if not isinstance(expected, dict):
        raise evidence.EvidenceError("Execution input snapshot is invalid")
    lock_project(project_id)
    db.session.expire_all()
    wanted_test_ids = _wanted_test_id_set(test_ids)
    current = _current(project_id, wanted_test_ids=wanted_test_ids)
    wanted_tests = _expected_rows(expected, "tests", ("uuid", "test_id", "steps"))
    if wanted_test_ids is not None:
        wanted_tests = [row for row in wanted_tests if row["test_id"] in wanted_test_ids]
    wanted = {
        "tests": wanted_tests,
        "lib": _expected_rows(expected, "lib", ("uuid", "name", "steps", "params", "init")),
        "const": _expected_rows(expected, "const", ("uuid", "name", "value")),
    }
    names = {"tests": "test", "lib": "lib", "const": "const"}
    conflicts = _test_conflicts(wanted["tests"], current["tests"])
    for key in ("lib", "const"):
        rows = wanted[key]
        if rows == current[key]:
            continue
        for index, row in enumerate(rows):
            current_row = current[key][index] if index < len(current[key]) else None
            if current_row != row:
                conflicts.append({"sheet": names[key], "uuid": row["uuid"]})
        for row in current[key][len(rows):]:
            conflicts.append({"sheet": names[key], "uuid": row["uuid"]})
    unique = []
    seen = set()
    for conflict in conflicts:
        identity = (conflict["sheet"], conflict["uuid"])
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(conflict)
    if unique:
        raise RunInputsNotMaterialized(unique)
