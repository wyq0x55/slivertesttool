"""Compare requested execution inputs with committed matrix rows."""

from __future__ import annotations

import json
from typing import Any

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


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return text
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


def _current(project_id: int) -> dict[str, list[dict[str, str]]]:
    return {
        "tests": [{
            "uuid": row.uuid or "",
            "test_id": export.row_test_id(row),
            "steps": _canonical(row.get_field(export.TEST_STEPS)),
        } for row in _rows(project_id, "test")],
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


def assert_materialized(project_id: int, expected: dict[str, Any]) -> None:
    """Lock the project and reject inputs that differ from the database."""
    if not isinstance(expected, dict):
        raise evidence.EvidenceError("Execution input snapshot is invalid")
    lock_project(project_id)
    db.session.expire_all()
    current = _current(project_id)
    wanted = {
        "tests": _expected_rows(expected, "tests", ("uuid", "test_id", "steps")),
        "lib": _expected_rows(expected, "lib", ("uuid", "name", "steps", "params", "init")),
        "const": _expected_rows(expected, "const", ("uuid", "name", "value")),
    }
    names = {"tests": "test", "lib": "lib", "const": "const"}
    conflicts = []
    for key, rows in wanted.items():
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
