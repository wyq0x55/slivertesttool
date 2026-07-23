"""Field catalogue and data-type coercion for the LAN Test Matrix.

Flask-independent so it can be unit-tested in isolation. Defines:

* :data:`DATA_TYPES` — the field data types supported by V1.0 (FR-GRID-003).
* :data:`TEST_FIELDS` / :data:`CONST_FIELDS` / :data:`LIB_FIELDS` — the field
  sets provisioned onto a project by the Test-Matrix / Const / Lib importers.
  Nothing is seeded at project creation: a new project starts with an empty
  table and no fields, and fields are created on import (or manually). Every
  field is fully editable and deletable.
* :func:`coerce_value` — normalise a raw (UI / Excel) value to its stored form.

Project fields live in ``field_definitions`` rows. Values for keys that map to a
first-class column (see ``TestItemRow._SYSTEM_COLUMN``) are stored on that column
transparently; all other field values are stored in ``test_items.custom_values``
(JSONB on PostgreSQL).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

# ``matrix_excel`` is the single source of truth for the Test-Matrix workbook
# layout (``SUMMARY_COLUMNS``). It is Flask-independent (openpyxl + config only),
# so importing it here keeps the template-field set derived from — and always in
# sync with — the Excel schema.
from . import matrix_excel as _mx

# --------------------------------------------------------------------------- #
# Data types (FR-GRID-003)
# --------------------------------------------------------------------------- #
DATA_TYPES: tuple[str, ...] = (
    "text",         # single-line text
    "multiline",    # multi-line text
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "single_select",
    "multi_select",
    "user",
    "url",
    "hex",          # hexadecimal number (e.g. CAN ID)
    "steps",        # test procedure (手順) block, edited via the step-table editor
    "computed",     # read-only computed (later versions)
)

# --------------------------------------------------------------------------- #
# Editor sheets / tabs
# --------------------------------------------------------------------------- #
# A project's fields and rows are partitioned across these named sheets. ``test``
# is the original Test-Matrix sheet; ``const`` and ``lib`` hold the imported
# constant / function-library tables. ``SHEET_STEPS_FIELD`` maps a sheet to the
# field that carries its 手順 (test-procedure) JSON, so the step editor knows
# which cell to edit per sheet.
SHEETS: tuple[str, ...] = ("test", "const", "lib")
DEFAULT_SHEET = "test"
SHEET_STEPS_FIELD: dict[str, str] = {"test": "steps", "lib": "lib_stb"}

# Display labels for the editor sheet tabs. Kept here so the whole sheet
# catalogue (keys, default, steps field, labels) has one source of truth that
# the ``/api/v1/config`` endpoint serves to the browser — the frontend no longer
# defines its own parallel ``SHEET_SPECS``.
SHEET_LABELS: dict[str, str] = {"test": "测试用例", "const": "常量", "lib": "函数库"}

# Naming conventions shared by the CRDT layer and the browser. ``ROW_ARRAY_PREFIX``
# builds the per-sheet ``Y.Array`` key (``rows:{sheet}``); ``ROOM_PREFIX`` builds
# the collaboration room name (``project:{id}``). Both are echoed by
# ``/api/v1/config`` so neither the CRDT doc model nor the frontend hard-codes
# them independently.
ROW_ARRAY_PREFIX = "rows:"
ROOM_PREFIX = "project:"


def sheet_row_key(sheet: str) -> str:
    """CRDT ``Y.Array`` key that holds ``sheet``'s rows (``rows:{sheet}``)."""
    return f"{ROW_ARRAY_PREFIX}{sheet}"


def room_name(project_id: int) -> str:
    """Collaboration room name for ``project_id`` (``project:{id}``)."""
    return f"{ROOM_PREFIX}{project_id}"


def matrix_config() -> dict[str, Any]:
    """Canonical editor/collab protocol config consumed by the frontend.

    Single source of truth for the sheet catalogue and CRDT/room naming, served
    verbatim by ``GET /api/v1/config`` so ``editor.js`` / ``collab.js`` never
    define a parallel schema.
    """
    return {
        "sheets": [{"key": k, "name": SHEET_LABELS.get(k, k)} for k in SHEETS],
        "default_sheet": DEFAULT_SHEET,
        "steps_fields": dict(SHEET_STEPS_FIELD),
        "row_array_prefix": ROW_ARRAY_PREFIX,
        "room_prefix": ROOM_PREFIX,
    }

MULTILINE_TYPES = frozenset({"multiline"})
SELECT_TYPES = frozenset({"single_select"})
MULTI_TYPES = frozenset({"multi_select"})
NUMERIC_TYPES = frozenset({"integer", "decimal", "hex"})


# --------------------------------------------------------------------------- #
# Test-Matrix import fields (NOT seeded — provisioned on import)
# --------------------------------------------------------------------------- #
# A new project starts completely empty: no seeded fields and no rows. The
# Test-Matrix ("test" sheet) field set below is provisioned onto the target
# project only when a 手順 workbook is first imported — exactly like the
# Const / Lib importers call :func:`fields_service.ensure_fields`. Nothing is
# hand-maintained: the list and order are derived directly from
# :data:`matrix_excel.SUMMARY_COLUMNS` (the single source of truth for the
# workbook layout), and each column's Japanese header becomes the field's
# display name — so importing an Excel table brings its columns in as project
# fields. ``priority`` / ``result`` still resolve to first-class row columns via
# ``TestItemRow._SYSTEM_COLUMN`` regardless of the field's (text) data type, and
# ``TM_TO_LM`` in the bridge stays a plain identity map.

# Procedure / steps block (input & expected signals + step table), stored as a
# JSON document and edited via the graphical step-table editor. It has no
# summary-sheet column, so it is appended after the derived columns.
_STEPS_FIELD: dict[str, Any] = {
    "field_key": "steps", "display_name": "测试步骤明细 (手順/JSON)",
    "data_type": "multiline", "sheet": "test",
}


def _build_test_fields() -> list[dict[str, Any]]:
    """Derive the Test-Matrix field set from ``matrix_excel.SUMMARY_COLUMNS``.

    Every summary column becomes a plain text field on the ``test`` sheet whose
    display name is the column's Japanese header; the 手順/steps block is
    appended last. Used by the Test-Matrix importer via ``ensure_fields``.
    """
    result: list[dict[str, Any]] = []
    for key, jp in _mx.SUMMARY_COLUMNS:
        result.append({
            "field_key": key,
            "display_name": jp or key,
            "data_type": "text",
            "sheet": "test",
        })
    result.append(dict(_STEPS_FIELD))
    return result


TEST_FIELDS: list[dict[str, Any]] = _build_test_fields()

TEST_FIELD_KEYS = frozenset(f["field_key"] for f in TEST_FIELDS)

# --------------------------------------------------------------------------- #
# Lib(Func) import fields
# --------------------------------------------------------------------------- #
# Field set for the function-library ("Lib") workbook, shown on the ``lib`` sheet.
# One function/row carries the metadata fields plus ``lib_stb`` — the 手順
# (test-procedure) block, which uses the SAME step-table editor / JSON document
# as the Test-Matrix ``steps`` field (data type ``steps``). All are ordinary
# project fields stored in ``test_items.custom_values`` — no DB schema change is
# needed. The Lib import ensures these definitions exist on the target project
# (on the ``lib`` sheet) before creating rows.
LIB_FIELDS: list[dict[str, Any]] = [
    {"field_key": "isinit", "display_name": "初始化函数 (isInit)", "data_type": "boolean", "sheet": "lib"},
    {"field_key": "lib_func", "display_name": "函数标识 (lib_func)", "data_type": "text", "sheet": "lib"},
    {"field_key": "lib_name", "display_name": "函数名称 (lib_name)", "data_type": "text", "sheet": "lib"},
    {"field_key": "lib_value", "display_name": "目的 (lib_value)", "data_type": "multiline", "sheet": "lib"},
    {"field_key": "lib_arg", "display_name": "引数 (lib_arg)", "data_type": "text", "sheet": "lib"},
    {"field_key": "lib_para", "display_name": "仮引数 (lib_para)", "data_type": "multiline", "sheet": "lib"},
    {"field_key": "lib_note", "display_name": "备考 (lib_note)", "data_type": "multiline", "sheet": "lib"},
    # 手順 block (input & expected signals + step table) — same JSON document and
    # step-table editor as the Test-Matrix ``steps`` field.
    {"field_key": "lib_stb", "display_name": "测试手顺 (lib_stb)", "data_type": "steps", "sheet": "lib"},
]
LIB_FIELD_KEYS = frozenset(f["field_key"] for f in LIB_FIELDS)

# --------------------------------------------------------------------------- #
# Const import fields
# --------------------------------------------------------------------------- #
# Field set for the constant-definition ("Const") workbook, shown on the
# ``const`` sheet (one row per constant). Stored in ``custom_values`` like every
# other project field. ``const_name`` is used as the row identity for upsert /
# replace-all import.
CONST_FIELDS: list[dict[str, Any]] = [
    {"field_key": "const_name", "display_name": "识别子名 (const_name)", "data_type": "text", "sheet": "const"},
    {"field_key": "const_jname", "display_name": "和名 (const_jname)", "data_type": "text", "sheet": "const"},
    {"field_key": "const_value", "display_name": "值 (const_value)", "data_type": "text", "sheet": "const"},
    {"field_key": "const_class1", "display_name": "分类1 (const_class1)", "data_type": "text", "sheet": "const"},
    {"field_key": "const_class2", "display_name": "分类2 (const_class2)", "data_type": "text", "sheet": "const"},
    {"field_key": "const_dataname", "display_name": "数据名称 (const_dataname)", "data_type": "text", "sheet": "const"},
    {"field_key": "const_note", "display_name": "备考 (const_note)", "data_type": "multiline", "sheet": "const"},
]
CONST_FIELD_KEYS = frozenset(f["field_key"] for f in CONST_FIELDS)

class CoercionError(ValueError):
    """Raised when a raw value cannot be coerced to its field data type."""


def coerce_value(data_type: str, raw: Any) -> Any:
    """Coerce a raw UI/Excel value to its canonical stored representation.

    Empty values normalise to ``None`` (except boolean/text which keep meaning).
    Raises :class:`CoercionError` when the value is structurally invalid; range
    and rule checks live in :mod:`app.services.lanmatrix.validation`.
    """
    if data_type not in DATA_TYPES:
        raise CoercionError(f"unknown data type: {data_type}")

    if raw is None:
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped == "" and data_type not in ("text", "multiline"):
            return None

    if data_type in ("text", "multiline", "url", "computed", "steps"):
        return raw if isinstance(raw, str) else str(raw)

    if data_type == "integer":
        try:
            if isinstance(raw, bool):
                raise CoercionError("bool is not an integer")
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise CoercionError(f"'{raw}' is not an integer")

    if data_type == "decimal":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise CoercionError(f"'{raw}' is not a number")

    if data_type == "hex":
        return _coerce_hex(raw)

    if data_type == "boolean":
        return _coerce_bool(raw)

    if data_type == "date":
        return _coerce_date(raw).isoformat()

    if data_type == "datetime":
        return _coerce_datetime(raw).isoformat()

    if data_type in ("single_select", "user"):
        return str(raw).strip() if not isinstance(raw, str) else raw.strip()

    if data_type == "multi_select":
        return _coerce_list(raw)

    return raw


def _coerce_hex(raw: Any) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if s in ("", "h"):
        raise CoercionError("empty hex value")
    if s.endswith("h"):
        s = s[:-1]
    try:
        return int(s, 16)
    except ValueError:
        raise CoercionError(f"'{raw}' is not a hexadecimal value")


def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "是"):
        return True
    if s in ("0", "false", "no", "n", "off", "否"):
        return False
    raise CoercionError(f"'{raw}' is not a boolean")


def _coerce_date(raw: Any) -> _dt.date:
    if isinstance(raw, _dt.datetime):
        return raw.date()
    if isinstance(raw, _dt.date):
        return raw
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise CoercionError(f"'{raw}' is not a valid date")


def _coerce_datetime(raw: Any) -> _dt.datetime:
    if isinstance(raw, _dt.datetime):
        return raw
    if isinstance(raw, _dt.date):
        return _dt.datetime(raw.year, raw.month, raw.day)
    s = str(raw).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise CoercionError(f"'{raw}' is not a valid datetime")


def _coerce_list(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    parts = [p.strip() for p in s.replace("；", ";").replace(",", ";").split(";")]
    return [p for p in parts if p]
