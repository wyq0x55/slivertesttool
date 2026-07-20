"""DB-facing import for the Lib(Func) and Const workbooks.

Both formats create editor rows (:class:`TestItemRow`) exactly like the
Test-Matrix bridge, storing their fields in ``test_items.custom_values`` — no DB
schema change. Row identity for upsert / replace-all is the row's ``case_id``
(``lib_Func`` for Lib, ``const_ident`` for Const), mirroring how the Test-Matrix
bridge keys on テストID.

The shared :func:`_run_import` provisions the format's field definitions on the
target project (``fields_service.ensure_fields``), optionally soft-deletes the
whole table (``replace_all``), then inserts / updates each parsed row.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import fields as fld

logger = logging.getLogger(__name__)

_MODES = ("upsert", "insert_only", "replace_all")


# --------------------------------------------------------------------------- #
# Pure value mapping
# --------------------------------------------------------------------------- #
def _steps_has_content(steps: Any) -> bool:
    if not isinstance(steps, dict):
        return False
    return bool(steps.get("steps") or steps.get("input_signals")
                or steps.get("expected_signals"))


def map_lib_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map one parsed Lib function block to lanmatrix field values (pure).

    ``lib_stb`` carries the 手順 (test-procedure) block. It arrives either as a
    structured dict (input/expected signals + step rows) — serialised to the
    same JSON document the step editor uses — or as a plain string cell.
    """
    values: dict[str, Any] = {}
    for key in ("isinit", "lib_func", "lib_name", "lib_value", "lib_arg",
                "lib_para", "lib_note"):
        val = item.get(key)
        if val is None or val == "":
            continue
        values[key] = val
    stb = item.get("lib_stb")
    if isinstance(stb, dict):
        if _steps_has_content(stb):
            values["lib_stb"] = json.dumps(stb, ensure_ascii=False, indent=2)
    elif stb not in (None, ""):
        values["lib_stb"] = stb
    return values


def map_const_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map one parsed Const row to lanmatrix field values (pure)."""
    values: dict[str, Any] = {}
    for key in ("const_name", "const_jname", "const_value", "const_class1",
                "const_class2", "const_dataname", "const_note"):
        val = item.get(key)
        if val is None or val == "":
            continue
        values[key] = val
    return values


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def import_libfunc(user, project, source, *, mode: str = "upsert",
                   original_filename: str = "") -> dict[str, Any]:
    from . import libfunc_excel, service

    try:
        parsed = libfunc_excel.parse_workbook(
            source, source_filename=original_filename)
    except libfunc_excel.LibExcelError as exc:
        raise service.ServiceError(f"Excel 解析失败：{exc}", code="IMPORT_PARSE_ERROR")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Lib import failed while parsing %r", original_filename)
        raise service.ServiceError(
            f"导入失败：无法解析该 Excel（{type(exc).__name__}: {exc}）。",
            code="IMPORT_PARSE_ERROR")

    items = parsed.get("items") or []
    if not items:
        raise service.ServiceError(
            "导入失败：未在文件中解析到任何 Lib 函数。请确认首行表头包含 "
            "isinit / lib_func / lib_name / lib_value / lib_para / lib_note / "
            "lib_stb 等列。",
            code="IMPORT_PARSE_ERROR")
    return _run_import(user, project, items, mode=mode, key_field="lib_func",
                       value_mapper=map_lib_item, field_specs=fld.LIB_FIELDS,
                       source_label=original_filename, sheet="lib")


def import_const(user, project, source, *, mode: str = "upsert",
                 original_filename: str = "") -> dict[str, Any]:
    from . import const_excel, service

    try:
        parsed = const_excel.parse_workbook(
            source, source_filename=original_filename)
    except const_excel.ConstExcelError as exc:
        raise service.ServiceError(f"Excel 解析失败：{exc}", code="IMPORT_PARSE_ERROR")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Const import failed while parsing %r", original_filename)
        raise service.ServiceError(
            f"导入失败：无法解析该 Excel（{type(exc).__name__}: {exc}）。",
            code="IMPORT_PARSE_ERROR")

    items = parsed.get("items") or []
    if not items:
        raise service.ServiceError(
            "导入失败：未在文件中解析到任何常量。请确认首行表头包含 "
            "const_name / const_value / const_note 列。",
            code="IMPORT_PARSE_ERROR")
    return _run_import(user, project, items, mode=mode, key_field="const_name",
                       value_mapper=map_const_item, field_specs=fld.CONST_FIELDS,
                       source_label=original_filename, sheet="const")


# --------------------------------------------------------------------------- #
# Shared import loop
# --------------------------------------------------------------------------- #
def _run_import(user, project, items: list[dict[str, Any]], *, mode: str,
                key_field: str, value_mapper: Callable[[dict], dict],
                field_specs: list[dict[str, Any]],
                source_label: str, sheet: str = "test") -> dict[str, Any]:
    import datetime as _dt

    from . import audit, fields_service, service
    from .testmatrix_bridge import _format_row_error
    from ...extensions import db
    from ...models import TestItemRow

    if mode not in _MODES:
        raise service.ServiceError(f"未知导入模式: {mode}", code="VALIDATION_ERROR")

    fields_service.ensure_fields(user, project, field_specs)

    # All row lookups / replace-all deletion are scoped to this import's sheet so
    # importing Const / Lib never touches the ``test`` sheet (or each other).
    deleted = 0
    if mode == "replace_all":
        now = _dt.datetime.now(_dt.timezone.utc)
        live = TestItemRow.query.filter_by(
            project_id=project.id, sheet=sheet, deleted_at=None).all()
        for row in live:
            row.deleted_at = now
        deleted = len(live)
        if deleted:
            audit.record("import.replace_all", actor_id=user.id,
                         object_type="import", project_id=project.id,
                         new_value={"deleted": deleted, "source": source_label,
                                    "sheet": sheet})
        db.session.commit()

    existing = {}
    if mode == "upsert":
        existing = {
            r.case_id: r for r in TestItemRow.query.filter_by(
                project_id=project.id, sheet=sheet, deleted_at=None).all()
        }

    created = updated = 0
    errors: list[dict] = []
    for idx, item in enumerate(items):
        values = value_mapper(item)
        key = str(item.get(key_field) or "").strip()
        if key:
            values["case_id"] = key
        try:
            target = existing.get(key) if (mode == "upsert" and key) else None
            if target is not None:
                changes = {k: v for k, v in values.items() if k != "case_id"}
                service.update_item(user, project, target, target.version, changes)
                updated += 1
            else:
                row = service.create_item(user, project, values, draft=True,
                                          sheet=sheet)
                if key:
                    existing[key] = row
                created += 1
        except service.ServiceError as exc:
            errors.append({"row": idx + 1, "case_id": key,
                           "message": _format_row_error(exc)})
        except Exception as exc:  # noqa: BLE001 - never abort the whole import
            logger.exception("Row %d import failed unexpectedly", idx + 1)
            errors.append({"row": idx + 1, "case_id": key,
                           "message": f"{type(exc).__name__}: {exc}"})

    return {
        "total": len(items),
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "mode": mode,
        "sheet": sheet,
        "errors": errors,
    }
