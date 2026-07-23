"""Bridge between the fixed Test-Matrix Excel layout and the LAN Test Matrix.

The v2.6 "Test Matrix" feature owns the Japanese
``VHILS1_RWS_MCU_Qualification_Test_SYS.xlsx`` layout (summary sheet + one
detail sheet per category). This module lets the generic online **Matrix
Editor** ingest that exact workbook — mapping each Japanese column onto the
editor's Test-Matrix based fields — and export the edited data back into the
same byte-compatible format, so the two sides round-trip.

The value-mapping helpers (:func:`map_item`, :func:`lm_to_tm`, priority/result
normalisation, :func:`reconstruct_case_id`) are deliberately Flask-independent
and pure so they can be unit-tested in isolation. The DB-touching entry points
(:func:`import_workbook`, :func:`export_workbook`) import the service layer and
the ``matrix_excel`` codec lazily.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("lanmatrix.import")

# --------------------------------------------------------------------------- #
# Column mapping: matrix_excel attribute key -> lanmatrix field key
# --------------------------------------------------------------------------- #
# Every Test-Matrix business column has a matching field provisioned on import
# (see ``fields.TEST_FIELDS``, also derived from
# ``matrix_excel.SUMMARY_COLUMNS``), so this is a plain identity map — there are no
# legacy remappings such as the old ``test_name -> title`` / ``remark -> comment``
# indirections. ``priority`` and ``result`` still need value translation
# (Japanese <-> option) and are handled by ``_VALUE_MAPPED`` below;
# ``priority``/``result`` continue to resolve to first-class row columns
# transparently via ``TestItemRow._SYSTEM_COLUMN``.
#
# The key list is derived directly from ``matrix_excel.SUMMARY_COLUMNS`` (the
# single source of truth for the workbook layout), minus the two
# workbook-calculated columns (``test_id`` / ``log``). Importing straight from
# that list means a column added to / removed from the Excel schema flows into
# the import mapping automatically — the bridge can never drift out of sync.
from .matrix_excel import SUMMARY_COLUMNS as _SUMMARY_COLUMNS

# Import every summary column, including the Excel-calculated ``test_id`` / ``log``
# (their cached values are read on parse and stored so the テストID / ログ columns
# are populated in the editor; export regenerates them as formulas).
_TM_KEYS: tuple[str, ...] = tuple(key for key, _jp in _SUMMARY_COLUMNS)
TM_TO_LM: dict[str, str] = {key: key for key in _TM_KEYS}
LM_TO_TM: dict[str, str] = {lm: tm for tm, lm in TM_TO_LM.items()}

# Keys handled specially (value translation) rather than a straight copy.
_VALUE_MAPPED = {"priority", "result"}

# 優先度 (Japanese) <-> lanmatrix priority options (Low/Medium/High/Critical).
PRIORITY_JP_TO_EN: dict[str, str] = {
    "高": "High", "中": "Medium", "低": "Low",
    "H": "High", "M": "Medium", "L": "Low",
    "High": "High", "Medium": "Medium", "Low": "Low", "Critical": "Critical",
}
PRIORITY_EN_TO_JP: dict[str, str] = {
    "High": "高", "Medium": "中", "Low": "低", "Critical": "高",
}

# 結果 (Japanese/free text) <-> lanmatrix result options.
RESULT_JP_TO_EN: dict[str, str] = {
    "": "Not Tested", "-": "Not Tested", "未実施": "Not Tested",
    "OK": "Pass", "合格": "Pass", "Pass": "Pass", "PASS": "Pass",
    "NG": "Fail", "不合格": "Fail", "Fail": "Fail", "FAIL": "Fail",
    "ブロック": "Blocked", "Blocked": "Blocked",
    "対象外": "N/A", "N/A": "N/A", "NA": "N/A",
}
RESULT_EN_TO_JP: dict[str, str] = {
    "Not Tested": "", "Pass": "OK", "Fail": "NG",
    "Blocked": "ブロック", "N/A": "対象外",
}

_EMPTY_STEPS = {"input_signals": [], "expected_signals": [], "steps": []}


# --------------------------------------------------------------------------- #
# Value normalisation (pure)
# --------------------------------------------------------------------------- #
def normalize_priority(raw: Any) -> str:
    if raw is None:
        return ""
    return PRIORITY_JP_TO_EN.get(str(raw).strip(), "")


def normalize_result(raw: Any) -> str:
    if raw is None:
        return "Not Tested"
    return RESULT_JP_TO_EN.get(str(raw).strip(), "Not Tested")


def _steps_has_content(steps: dict) -> bool:
    if not isinstance(steps, dict):
        return False
    return bool(steps.get("steps") or steps.get("input_signals")
                or steps.get("expected_signals"))


def map_item(tm_item: dict[str, Any]) -> dict[str, Any]:
    """Map one parsed Test-Matrix item to lanmatrix field values (pure)."""
    values: dict[str, Any] = {}
    for tm_key, lm_key in TM_TO_LM.items():
        if tm_key in _VALUE_MAPPED:
            continue
        val = tm_item.get(tm_key)
        if val is None or val == "":
            continue
        values[lm_key] = val

    priority = normalize_priority(tm_item.get("priority"))
    if priority:
        values["priority"] = priority
    values["result"] = normalize_result(tm_item.get("result"))

    steps = tm_item.get("steps") or {}
    if _steps_has_content(steps):
        values["steps"] = json.dumps(steps, ensure_ascii=False, indent=2)
    return values


def reconstruct_case_id(id_prefix: str, tm_item: dict[str, Any]) -> str:
    """Rebuild the deterministic テストID (prefix + cat3 + no3), or ''."""
    cat = tm_item.get("category")
    no = tm_item.get("test_no")
    if cat is None or no is None:
        return ""
    try:
        return f"{id_prefix or ''}{int(cat):03d}{int(no):03d}"
    except (TypeError, ValueError):
        return ""


def parse_steps(raw: Any) -> dict[str, Any]:
    """Parse a stored ``steps`` cell (JSON string) into the steps dict."""
    if isinstance(raw, dict):
        base = dict(_EMPTY_STEPS)
        base.update(raw)
        return base
    if isinstance(raw, str) and raw.strip():
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            return dict(_EMPTY_STEPS)
        if isinstance(doc, dict):
            base = dict(_EMPTY_STEPS)
            base.update(doc)
            return base
    return dict(_EMPTY_STEPS)


def lm_to_tm(row: dict[str, Any]) -> dict[str, Any]:
    """Map one lanmatrix item dict back to a Test-Matrix item dict (pure)."""
    tm: dict[str, Any] = {}
    for tm_key, lm_key in TM_TO_LM.items():
        if tm_key in _VALUE_MAPPED:
            continue
        val = row.get(lm_key)
        tm[tm_key] = "" if val is None else val
    tm["priority"] = PRIORITY_EN_TO_JP.get(
        row.get("priority"), row.get("priority") or "")
    tm["result"] = RESULT_EN_TO_JP.get(row.get("result"), row.get("result") or "")
    tm["steps"] = parse_steps(row.get("steps"))
    return tm


# --------------------------------------------------------------------------- #
# DB-facing entry points (Flask/SQLAlchemy — imported lazily)
# --------------------------------------------------------------------------- #
def _format_row_error(exc) -> str:
    """Turn a per-row ServiceError into a human-readable reason.

    Field validation raises a generic ``"输入数据校验失败"`` message whose real
    cause lives in ``exc.details`` (a list of ``{field, message, severity}``).
    Expand those field-level messages so the user sees exactly what failed.
    """
    details = getattr(exc, "details", None)
    if isinstance(details, (list, tuple)) and details:
        parts = []
        for d in details:
            if isinstance(d, dict):
                fld = d.get("field") or d.get("display_name") or ""
                msg = d.get("message") or ""
                parts.append(f"{fld}：{msg}" if fld else msg)
            else:
                parts.append(str(d))
        joined = "；".join(p for p in parts if p)
        if joined:
            return f"{exc}（{joined}）"
    return str(exc)


def import_workbook(user, project, source, *, mode: str = "upsert",
                    original_filename: str = "") -> dict[str, Any]:
    """Parse a Test-Matrix workbook and create/update editor items.

    ``mode``:

    * ``upsert`` — match existing rows by テストID, else insert.
    * ``insert_only`` — always insert new rows.
    * ``replace_all`` — soft-delete every existing row in the project first, then
      insert all rows from the workbook (a whole-table replacement, mirroring the
      generic ``excel_service`` import). Requires the ``import.replace`` permission,
      which the calling route enforces.

    Returns a summary dict.
    """
    import datetime as _dt

    from . import audit, fields as fld, fields_service, matrix_excel, service
    from ...extensions import db
    from ...models import TestItemRow

    if mode not in ("upsert", "insert_only", "replace_all"):
        raise service.ServiceError(f"未知导入模式: {mode}", code="VALIDATION_ERROR")

    try:
        matrix = matrix_excel.parse_workbook(
            source, source_filename=original_filename)
    except matrix_excel.MatrixExcelError as exc:
        raise service.ServiceError(f"Excel 解析失败：{exc}", code="IMPORT_PARSE_ERROR")
    except Exception as exc:  # noqa: BLE001 - surface any parse failure as a reason
        logger.exception("Test-matrix import failed while parsing %r",
                         original_filename or source)
        raise service.ServiceError(
            f"导入失败：无法解析该 Excel（{type(exc).__name__}: {exc}）。"
            f"请确认文件包含设定 sheet「{matrix_excel.DEFAULT_SUMMARY_SHEET}」"
            f"及对应的纯数字明细 sheet。",
            code="IMPORT_PARSE_ERROR")

    items = matrix.get("items") or []
    if not items:
        raise service.ServiceError(
            f"导入失败：未在文件中解析到任何测试项。"
            f"请确认设定 sheet「{matrix.get('summary_sheet') or matrix_excel.DEFAULT_SUMMARY_SHEET}」"
            f"含有 テスト区分 / テスト番号 表头及数据行。",
            code="IMPORT_PARSE_ERROR")
    id_prefix = matrix.get("id_prefix") or matrix_excel.DEFAULT_ID_PREFIX
    _save_project_meta(project, matrix)

    # Provision the Test-Matrix field set on the target project before creating
    # rows — mirroring the Const / Lib importers. A new project starts with no
    # fields, so without this the imported column headers and values would have
    # nowhere to be stored (``create_item`` only keeps values whose field exists).
    fields_service.ensure_fields(user, project, fld.TEST_FIELDS)

    # ``replace_all``: clear the whole table before inserting. Soft-delete every
    # live row in one shot (same semantics as ``excel_service`` replace_all) so
    # the freshly parsed workbook fully supersedes the previous content. This is
    # committed up-front so the subsequent per-row inserts can reuse the same
    # テストID values without colliding with the old rows.
    deleted = 0
    if mode == "replace_all":
        now = _dt.datetime.now(_dt.timezone.utc)
        live = TestItemRow.query.filter_by(
            project_id=project.id, deleted_at=None).all()
        for row in live:
            row.deleted_at = now
        deleted = len(live)
        if deleted:
            audit.record("import.replace_all", actor_id=user.id,
                         object_type="import", project_id=project.id,
                         new_value={"deleted": deleted,
                                    "source": original_filename})
        db.session.commit()

    existing = {}
    if mode == "upsert":
        existing = {
            r.case_id: r for r in TestItemRow.query.filter_by(
                project_id=project.id, deleted_at=None).all()
        }

    created = updated = 0
    errors: list[dict] = []
    for idx, tm in enumerate(items):
        values = map_item(tm)
        case_id = reconstruct_case_id(id_prefix, tm)
        if case_id:
            values["case_id"] = case_id
            # If the workbook's テストID cell had no cached value, fall back to the
            # reconstructed id so the visible test_id column is never blank.
            if not values.get("test_id"):
                values["test_id"] = case_id
        try:
            target = existing.get(case_id) if (mode == "upsert" and case_id) else None
            if target is not None:
                changes = {k: v for k, v in values.items() if k != "case_id"}
                service.update_item(user, project, target, target.version, changes)
                updated += 1
            else:
                item = service.create_item(user, project, values, draft=True)
                if case_id:
                    existing[case_id] = item
                created += 1
        except service.ServiceError as exc:
            errors.append({
                "row": idx + 1,
                "case_id": case_id or values.get("test_name") or "",
                "message": _format_row_error(exc),
            })
        except Exception as exc:  # noqa: BLE001 - never abort the whole import
            logger.exception("Row %d import failed unexpectedly", idx + 1)
            errors.append({
                "row": idx + 1,
                "case_id": case_id or values.get("test_name") or "",
                "message": f"{type(exc).__name__}: {exc}",
            })

    return {
        "total": len(items),
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "mode": mode,
        "errors": errors,
        "id_prefix": id_prefix,
        "summary_sheet": matrix.get("summary_sheet"),
    }


def export_workbook(project):
    """Rebuild a byte-compatible Test-Matrix ``.xlsx`` from editor items."""
    import io

    from . import matrix_excel
    from ...models import TestItemRow

    rows = TestItemRow.query.filter_by(
        project_id=project.id, deleted_at=None
    ).order_by(TestItemRow.row_order).all()
    items = [lm_to_tm(r.to_dict()) for r in rows]
    matrix = {
        "id_prefix": _project_meta(project, "tm_id_prefix")
        or matrix_excel.DEFAULT_ID_PREFIX,
        "summary_sheet": _project_meta(project, "tm_summary_sheet")
        or matrix_excel.DEFAULT_SUMMARY_SHEET,
        "items": items,
    }
    wb = matrix_excel.build_workbook(matrix)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _project_meta(project, attr: str) -> Optional[str]:
    return getattr(project, attr, None)


def _save_project_meta(project, matrix: dict[str, Any]) -> None:
    """Persist the workbook's id prefix / summary sheet for export parity."""
    from ...extensions import db

    changed = False
    if hasattr(project, "tm_id_prefix"):
        prefix = matrix.get("id_prefix")
        if prefix and project.tm_id_prefix != prefix:
            project.tm_id_prefix = prefix
            changed = True
    if hasattr(project, "tm_summary_sheet"):
        sheet = matrix.get("summary_sheet")
        if sheet and project.tm_summary_sheet != sheet:
            project.tm_summary_sheet = sheet
            changed = True
    if changed:
        db.session.add(project)
        db.session.flush()
