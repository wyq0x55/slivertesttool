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


def map_io_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map one parsed 入出力 row to lanmatrix field values (pure)."""
    values: dict[str, Any] = {}
    for key in ("io_name", "io_path", "io_note"):
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


def import_io(user, project, source, *, mode: str = "upsert",
              original_filename: str = "") -> dict[str, Any]:
    """Import an 入出力 (I/O signal pool) workbook.

    Unlike Lib / Const, the 入出力 pool enforces a two-column uniqueness contract
    (``io_name`` AND ``io_path`` unique) so a copied ``名称(路径)`` token never
    resolves ambiguously — see :func:`_run_io_import`.
    """
    from . import io_excel, service

    try:
        parsed = io_excel.parse_workbook(
            source, source_filename=original_filename)
    except io_excel.IoExcelError as exc:
        raise service.ServiceError(f"Excel 解析失败：{exc}", code="IMPORT_PARSE_ERROR")
    except Exception as exc:  # noqa: BLE001
        logger.exception("IO import failed while parsing %r", original_filename)
        raise service.ServiceError(
            f"导入失败：无法解析该 Excel（{type(exc).__name__}: {exc}）。",
            code="IMPORT_PARSE_ERROR")

    items = parsed.get("items") or []
    if not items:
        raise service.ServiceError(
            "导入失败：未在文件中解析到任何入出力信号。请确认首行表头包含 "
            "io_name / io_path（名称 / 路径）列。",
            code="IMPORT_PARSE_ERROR")
    return _run_io_import(user, project, items, mode=mode,
                          source_label=original_filename)


# --------------------------------------------------------------------------- #
# Export (editor rows -> workbook bytes)
# --------------------------------------------------------------------------- #
def _export_rows(project, sheet: str) -> list[dict[str, Any]]:
    from ...models import TestItemRow

    rows = TestItemRow.query.filter_by(
        project_id=project.id, sheet=sheet, deleted_at=None
    ).order_by(TestItemRow.row_order).all()
    return [r.to_dict() for r in rows]


def export_libfunc(project):
    """Rebuild a Lib(Func) ``.xlsx`` from the project's ``lib`` sheet rows."""
    import io

    from . import libfunc_excel

    items = _export_rows(project, "lib")
    wb = libfunc_excel.build_workbook({"items": items})
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_const(project):
    """Rebuild a Const ``.xlsx`` from the project's ``const`` sheet rows."""
    import io

    from . import const_excel

    items = _export_rows(project, "const")
    wb = const_excel.build_workbook({"items": items})
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_io(project):
    """Rebuild an 入出力 ``.xlsx`` from the project's ``io`` sheet rows."""
    import io

    from . import io_excel

    items = _export_rows(project, "io")
    wb = io_excel.build_workbook({"items": items})
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# --------------------------------------------------------------------------- #
# Extract I/O signals from step procedures (VHILS 手順 -> 入出力 pool)
# --------------------------------------------------------------------------- #
_EXTRACT_SHEETS = ("lib", "test")


def _extract_pairs(signals: Any) -> list[tuple[str, str]]:
    """Normalise an ``input_signals`` / ``expected_signals`` list to (name, path).

    Mirrors ``silver_json_export._signal_pairs`` but is kept Flask-free here so
    the pure collection logic is unit-testable in isolation. Accepts the three
    persisted shapes: ``[name, path]`` pairs, ``{"name", "path"}`` dicts, and a
    bare scalar (treated as a name with no path).
    """
    out: list[tuple[str, str]] = []
    for sig in signals if isinstance(signals, list) else []:
        if isinstance(sig, (list, tuple)):
            name = "" if len(sig) < 1 or sig[0] is None else str(sig[0])
            path = "" if len(sig) < 2 or sig[1] is None else str(sig[1])
        elif isinstance(sig, dict):
            name = str(sig.get("name") or "")
            path = str(sig.get("path") or "")
        else:
            name, path = str(sig or ""), ""
        out.append((name, path))
    return out


def collect_io_signals(bodies) -> dict[str, Any]:
    """Collect + de-duplicate I/O signals from step-doc bodies (pure).

    ``bodies`` is any iterable of parsed step-doc dicts, each carrying
    ``input_signals`` / ``expected_signals`` lists of ``[name, path]`` pairs.
    De-duplication keys on ``(name, path)`` case-insensitively (matching the
    pool's uniqueness normalisation) and preserves first-seen order. Signals
    with only a path (no name) cannot key the name-unique pool and are counted
    as ``skipped_nameless`` rather than emitted.

    Returns ``{"items", "scanned_rows", "distinct_signals", "skipped_nameless"}``
    where ``items`` are ``{"io_name", "io_path"}`` dicts ready for
    :func:`_run_io_import` (``io_note`` intentionally omitted so an upsert never
    clobbers an existing note).
    """
    scanned = skipped = 0
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for body in bodies:
        scanned += 1
        if not isinstance(body, dict):
            continue
        pairs = (_extract_pairs(body.get("input_signals"))
                 + _extract_pairs(body.get("expected_signals")))
        for name, path in pairs:
            name = str(name or "").strip()
            path = str(path or "").strip()
            if not name and not path:
                continue
            key = (name.lower(), path.lower())
            if key in seen:
                continue
            seen.add(key)
            if not name:
                skipped += 1
                continue
            items.append({"io_name": name, "io_path": path})
    return {"items": items, "scanned_rows": scanned,
            "distinct_signals": len(items), "skipped_nameless": skipped}


def extract_io_from_steps(user, project, *, sheets=("lib",), mode: str = "upsert",
                          source_label: str = "extract") -> dict[str, Any]:
    """Harvest I/O signal declarations from step procedures into the pool.

    Every ``lib`` / ``test`` row stores its procedure in a ``steps`` field whose
    body carries ``input_signals`` / ``expected_signals`` — lists of
    ``[name, path]`` pairs. This scans the requested sheets, collects every
    declared signal (:func:`collect_io_signals`), and funnels the de-duplicated
    result through :func:`_run_io_import` so the pool's name+path uniqueness
    contract and its per-row error reporting apply unchanged.

    Returns the :func:`_run_io_import` summary augmented with extraction stats:
    ``scanned_rows``, ``distinct_signals`` and ``skipped_nameless`` (declarations
    that carry only a path and so cannot key the name-unique pool).
    """
    from . import service
    from .silver_json_export import _parse_json_field
    from ...models import TestItemRow

    if mode not in _MODES:
        raise service.ServiceError(f"未知导入模式: {mode}", code="VALIDATION_ERROR")

    step_fields = fld.SHEET_STEPS_FIELD
    wanted = [s for s in sheets if s in step_fields]
    if not wanted:
        raise service.ServiceError(
            "抽取失败：仅支持从含手順的表（lib / test）抽取入出力。",
            code="VALIDATION_ERROR")

    bodies: list[Any] = []
    for sheet in wanted:
        field_key = step_fields[sheet]
        rows = TestItemRow.query.filter_by(
            project_id=project.id, sheet=sheet, deleted_at=None
        ).order_by(TestItemRow.row_order).all()
        for row in rows:
            bodies.append(_parse_json_field(row.get_field(field_key)))

    collected = collect_io_signals(bodies)
    summary = _run_io_import(user, project, collected["items"], mode=mode,
                             source_label=source_label)
    summary["scanned_rows"] = collected["scanned_rows"]
    summary["distinct_signals"] = collected["distinct_signals"]
    summary["skipped_nameless"] = collected["skipped_nameless"]
    summary["sheets"] = wanted
    return summary


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


# --------------------------------------------------------------------------- #
# 入出力 import loop (name + path uniqueness)
# --------------------------------------------------------------------------- #
def _run_io_import(user, project, items: list[dict[str, Any]], *, mode: str,
                   source_label: str) -> dict[str, Any]:
    """Import the 入出力 pool, keeping ``io_name`` AND ``io_path`` unique.

    Upsert keys on ``io_name`` (case-insensitive). A row is rejected — with a
    per-row error, never aborting the whole import — when its name or path
    collides with another pool row (or an earlier row in the same file), except
    the one it upserts onto. Mirrors the result shape of :func:`_run_import`.
    """
    import datetime as _dt

    from . import audit, fields_service, service
    from .testmatrix_bridge import _format_row_error
    from ...extensions import db
    from ...models import TestItemRow

    if mode not in _MODES:
        raise service.ServiceError(f"未知导入模式: {mode}", code="VALIDATION_ERROR")

    fields_service.ensure_fields(user, project, fld.IO_FIELDS)

    def _norm(v: Any) -> str:
        return str(v or "").strip().lower()

    deleted = 0
    if mode == "replace_all":
        now = _dt.datetime.now(_dt.timezone.utc)
        live = TestItemRow.query.filter_by(
            project_id=project.id, sheet="io", deleted_at=None).all()
        for row in live:
            row.deleted_at = now
        deleted = len(live)
        if deleted:
            audit.record("import.replace_all", actor_id=user.id,
                         object_type="import", project_id=project.id,
                         new_value={"deleted": deleted, "source": source_label,
                                    "sheet": "io"})
        db.session.commit()

    # Live pool after any replace_all: indexes for uniqueness + upsert lookup.
    live_rows = TestItemRow.query.filter_by(
        project_id=project.id, sheet="io", deleted_at=None).all()
    by_name: dict[str, TestItemRow] = {}
    path_owner: dict[str, TestItemRow] = {}
    for r in live_rows:
        nm = _norm(r.get_field("io_name"))
        pt = _norm(r.get_field("io_path"))
        if nm and nm not in by_name:
            by_name[nm] = r
        if pt and pt not in path_owner:
            path_owner[pt] = r

    created = updated = 0
    errors: list[dict] = []
    for idx, item in enumerate(items):
        values = map_io_item(item)
        name = str(values.get("io_name") or "").strip()
        path = str(values.get("io_path") or "").strip()
        nm, pt = _norm(name), _norm(path)
        if not name:
            errors.append({"row": idx + 1, "case_id": "",
                           "message": "名称不能为空"})
            continue
        target = by_name.get(nm) if mode == "upsert" else None
        if mode == "insert_only" and nm in by_name:
            errors.append({"row": idx + 1, "case_id": name,
                           "message": f"名称已存在：{name}"})
            continue
        # Path uniqueness: a path may only belong to the row we're upserting onto.
        if pt:
            owner = path_owner.get(pt)
            if owner is not None and owner is not target:
                errors.append({"row": idx + 1, "case_id": name,
                               "message": f"路径已存在：{path}"})
                continue
        values["case_id"] = name
        try:
            if target is not None:
                # Drop this row's old path claim before re-registering below.
                old_pt = _norm(target.get_field("io_path"))
                if old_pt and path_owner.get(old_pt) is target:
                    path_owner.pop(old_pt, None)
                changes = {k: v for k, v in values.items() if k != "case_id"}
                service.update_item(user, project, target, target.version, changes)
                updated += 1
            else:
                row = service.create_item(user, project, values, draft=True,
                                          sheet="io")
                by_name[nm] = row
                target = row
                created += 1
            if pt:
                path_owner[pt] = target
        except service.ServiceError as exc:
            errors.append({"row": idx + 1, "case_id": name,
                           "message": _format_row_error(exc)})
        except Exception as exc:  # noqa: BLE001 - never abort the whole import
            logger.exception("IO row %d import failed unexpectedly", idx + 1)
            errors.append({"row": idx + 1, "case_id": name,
                           "message": f"{type(exc).__name__}: {exc}"})

    return {
        "total": len(items),
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "mode": mode,
        "sheet": "io",
        "errors": errors,
    }
