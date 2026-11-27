"""Parser/writer for the ``4.TestRequirement`` multi-book workbook layout.

The workbook is close to the legacy Test-Matrix, but it adds ``シート名``,
``項目ID`` and ``グループID`` and uses one block per test id in the numeric
detail sheets. Block headers therefore carry the test id in column B and the
item id in column D (they do not start with ``ID;;``), so this module keeps the
book layout separate instead of weakening the legacy parser.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import re
from pathlib import Path
from typing import Any, BinaryIO, Optional, Union

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import matrix_excel as _mx

SUMMARY_SHEET = "4.TestRequirement"

SUMMARY_COLUMNS: list[tuple[str, str]] = [
    ("test_id", "テストID"),
    ("category", "テスト区分"),
    ("sheet_name", "シート名"),
    ("viewpoint", "テスト観点"),
    ("test_no", "テスト番号"),
    ("item_id", "項目ID ※「-」を「_」に変換。ID;;を削除"),
    ("purpose", "目的"),
    ("environment", "環境"),
    ("env_version", "環境バージョン"),
    ("data_flash", "データフラッシュ"),
    ("parameter", "パラメータ"),
    ("priority", "優先度"),
    ("description", "説明"),
    ("item_created", "項目作成"),
    ("exec_date", "実施日"),
    ("executor", "実施者"),
    ("version_label", "バージョン"),
    ("log", "ログ"),
    ("result", "結果"),
    ("remark", "備考"),
    ("traceability_id", "トレーサビリティID"),
    ("upper_req_id", "上位要求ID"),
    ("group_id", "グループID"),
]

SUMMARY_HEADERS = [header for _key, header in SUMMARY_COLUMNS]

DETAIL_LABELS: list[tuple[str, str]] = [
    ("目的", "purpose"),
    ("環境", "environment"),
    ("環境バージョン", "env_version"),
    ("データフラッシュ", "data_flash"),
    ("パラメータ", "parameter"),
    ("優先度", "priority"),
    ("説明", "description"),
    ("実施日", "exec_date"),
    ("実施者", "executor"),
    ("バージョン", "version_label"),
    ("ログ", "log"),
    ("結果", "result"),
    ("備考", "remark"),
]
_DETAIL_LABEL_NAMES = {label for label, _key in DETAIL_LABELS}

_COL_TEST_ID = 2
_COL_ITEM_ID = 4
_COL_LABEL = 2
_COL_STEP_NO = _mx.COL_STEP_NO
_COL_STEP_PURPOSE = _mx.COL_STEP_PURPOSE
_COL_STEP_OP = _mx.COL_STEP_OP
_COL_STEP_SUB = _mx.COL_STEP_SUB
_COL_STEP_ARG = _mx.COL_STEP_ARG
_COL_SIGNAL_START = _mx.COL_SIGNAL_START


class BookExcelError(Exception):
    """User-facing parse error for a specification book workbook."""


def _as_seekable(source: Union[str, BinaryIO]) -> Union[str, io.BytesIO]:
    if isinstance(source, (str, Path)):
        return str(source)
    if hasattr(source, "seek"):
        source.seek(0)
        return source
    return io.BytesIO(source.read())


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _cell_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _to_int(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else value


def _header_key(value: Any) -> str:
    text = _text(value).strip()
    if text.startswith("項目ID"):
        return "item_id"
    for key, header in SUMMARY_COLUMNS:
        if text == header:
            return key
    return ""


def _detect_header(ws) -> tuple[int, dict[str, int]]:
    for row in range(1, min(ws.max_row, 12) + 1):
        columns: dict[str, int] = {}
        for col in range(1, ws.max_column + 1):
            key = _header_key(ws.cell(row, col).value)
            if key:
                columns.setdefault(key, col)
        if ("sheet_name" not in columns
                and "category" in columns and "viewpoint" in columns
                and columns["viewpoint"] == columns["category"] + 2):
            columns["sheet_name"] = columns["category"] + 1
        if {"test_id", "category", "sheet_name", "test_no", "item_id"} <= set(columns):
            return row, columns
    raise BookExcelError(
        f"缺少设定 sheet「{SUMMARY_SHEET}」的 テストID / テスト区分 / "
        f"シート名 / テスト番号 / 項目ID 表头"
    )


def _is_block_header(ws, row: int) -> bool:
    value = ws.cell(row, _COL_TEST_ID).value
    item_id = ws.cell(row, _COL_ITEM_ID).value
    if value is None or item_id in (None, ""):
        return False
    if not isinstance(value, str):
        return False
    if value.strip() in _DETAIL_LABEL_NAMES:
        return False
    # The header is immediately followed by the first metadata label. Step rows
    # never satisfy this, and a numeric category sheet has no other non-label
    # value in column B with a populated item id in column D.
    if row >= ws.max_row:
        return False
    return _text(ws.cell(row + 1, _COL_LABEL).value).strip() == DETAIL_LABELS[0][0]


def _next_block_row(ws, start: int) -> Optional[int]:
    for row in range(start, ws.max_row + 1):
        if _is_block_header(ws, row):
            return row
    return None


def _parse_detail_sheet(ws, out: dict[str, dict[str, Any]]) -> None:
    row = 1
    while row <= ws.max_row:
        if not _is_block_header(ws, row):
            row += 1
            continue
        test_id = _text(ws.cell(row, _COL_TEST_ID).value).strip()
        item_id = _text(ws.cell(row, _COL_ITEM_ID).value).strip()
        step_header = None
        for candidate in range(row + 1, min(row + 40, ws.max_row) + 1):
            if ws.cell(candidate, _COL_STEP_NO).value == "手順番号":
                step_header = candidate
                break
        payload: dict[str, Any] = {
            "input_signals": [], "expected_signals": [], "steps": [],
        }
        if step_header is not None:
            input_signals, expected_signals, steps = _mx.parse_step_table(
                ws, step_header,
                stop_on_block=lambda sheet, r: _is_block_header(sheet, r),
            )
            payload = {
                "input_signals": input_signals,
                "expected_signals": expected_signals,
                "steps": steps,
            }
        if test_id:
            out[test_id] = payload
        elif item_id:
            out[item_id] = payload
        next_row = _next_block_row(ws, row + 1)
        row = next_row if next_row is not None else ws.max_row + 1


def parse_workbook(source: Union[str, BinaryIO], *,
                   source_filename: str = "") -> dict[str, Any]:
    """Read one specification book workbook into editor item dicts."""
    try:
        wb = load_workbook(_as_seekable(source), data_only=True, read_only=False)
    except Exception as exc:  # noqa: BLE001 - parser input boundary
        raise BookExcelError(f"无法打开 Excel：{exc}") from exc

    if SUMMARY_SHEET in wb.sheetnames:
        summary = wb[SUMMARY_SHEET]
    else:
        summary = next((wb[name] for name in wb.sheetnames
                        if name.endswith("TestRequirement")), None)
    if summary is None:
        raise BookExcelError(f"未找到设定 sheet「{SUMMARY_SHEET}」")

    header_row, columns = _detect_header(summary)
    details: dict[str, dict[str, Any]] = {}
    for name in wb.sheetnames:
        if not name.isdigit():
            continue
        _parse_detail_sheet(wb[name], details)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in range(header_row + 1, summary.max_row + 1):
        values = {
            key: _cell_or_none(summary.cell(row, columns[key]).value)
            for key in columns
        }
        test_id = _text(values.get("test_id")).strip()
        if not test_id:
            continue
        if test_id in seen:
            raise BookExcelError(f"同一本书内 テストID 重复：{test_id}")
        seen.add(test_id)
        item: dict[str, Any] = dict(values)
        item["test_id"] = test_id
        item["category"] = _to_int(item.get("category"))
        item["test_no"] = _to_int(item.get("test_no"))
        item["sheet_name"] = _text(item.get("sheet_name")).strip()
        item["item_id"] = _text(item.get("item_id")).strip()
        item["steps"] = details.get(test_id) or {}
        items.append(item)

    return {
        "items": items,
        "summary_sheet": summary.title,
        "source_filename": source_filename or "",
        "book_name": Path(source_filename).stem if source_filename else "",
    }


def _display(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "-"
    return value


def _steps_doc(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _write_summary(ws, items: list[dict[str, Any]]) -> None:
    ws.append(SUMMARY_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAD3")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for index, item in enumerate(items, start=2):
        for col, (key, _header) in enumerate(SUMMARY_COLUMNS, start=1):
            value = item.get(key)
            if key in ("category", "test_no"):
                value = _to_int(value)
            ws.cell(index, col).value = _display(value)
    for col, width in enumerate(
            [34, 9, 24, 36, 9, 52, 24, 10, 12, 20, 20, 8, 24, 12, 12, 12,
             10, 34, 10, 24, 44, 44, 12], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"


def _write_detail_sheet(ws, items: list[dict[str, Any]]) -> None:
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 42
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 52
    ws.column_dimensions["E"].width = 30
    ws.column_dimensions["F"].width = 24
    for col in range(_COL_SIGNAL_START, _COL_SIGNAL_START + 18):
        ws.column_dimensions[get_column_letter(col)].width = 20
    row = 1
    for item in items:
        steps = _steps_doc(item.get("steps"))
        if not (steps.get("steps") or steps.get("input_signals")
                or steps.get("expected_signals")):
            continue
        ws.cell(row, 1).value = item.get("sheet_name") or item.get("test_id")
        header_row = row + 2
        ws.cell(header_row, _COL_TEST_ID).value = item.get("test_id")
        ws.cell(header_row, _COL_ITEM_ID).value = item.get("item_id")
        for offset, (label, key) in enumerate(DETAIL_LABELS, start=1):
            ws.cell(header_row + offset, _COL_LABEL).value = label
            ws.cell(header_row + offset, 3).value = ": " + _text(
                _display(item.get(key)))
        step_header = header_row + len(DETAIL_LABELS) + 2
        headers = ["手順番号", "手順目的", "操作手順", "サブルーチン", "引数"]
        for index, value in enumerate(headers, start=_COL_STEP_NO):
            ws.cell(step_header, index).value = value
        inputs = steps.get("input_signals") or []
        expecteds = steps.get("expected_signals") or []
        for index in range(len(inputs)):
            ws.cell(step_header, _COL_SIGNAL_START + index).value = "入力値"
        for index in range(len(expecteds)):
            ws.cell(step_header, _COL_SIGNAL_START + len(inputs) + index).value = "期待値"
        ws.cell(step_header, _COL_SIGNAL_START + len(inputs) + len(expecteds)).value = "確認タイミング"
        for sig_row, values in ((step_header + 1, [pair[0] if pair else "" for pair in inputs]),
                                (step_header + 2, [pair[1] if len(pair) > 1 else "" for pair in inputs])):
            for index, value in enumerate(values):
                ws.cell(sig_row, _COL_SIGNAL_START + index).value = value
        for sig_row, values in ((step_header + 1, [pair[0] if pair else "" for pair in expecteds]),
                                (step_header + 2, [pair[1] if len(pair) > 1 else "" for pair in expecteds])):
            for index, value in enumerate(values):
                ws.cell(sig_row, _COL_SIGNAL_START + len(inputs) + index).value = value
        step_row = step_header + 3
        for step in steps.get("steps") or []:
            ws.cell(step_row, _COL_STEP_NO).value = step.get("no")
            ws.cell(step_row, _COL_STEP_PURPOSE).value = step.get("purpose")
            ws.cell(step_row, _COL_STEP_OP).value = step.get("operation")
            ws.cell(step_row, _COL_STEP_SUB).value = step.get("subroutine")
            ws.cell(step_row, _COL_STEP_ARG).value = step.get("args")
            for index, value in enumerate(step.get("inputs") or []):
                ws.cell(step_row, _COL_SIGNAL_START + index).value = value
            for index, value in enumerate(step.get("expecteds") or []):
                ws.cell(step_row, _COL_SIGNAL_START + len(inputs) + index).value = value
            ws.cell(step_row, _COL_SIGNAL_START + len(inputs) + len(expecteds)).value = step.get("timing")
            step_row += 1
        header_cell = ws.cell(step_header, _COL_STEP_NO)
        header_cell.font = Font(bold=True)
        for cell in ws[step_header]:
            cell.fill = PatternFill("solid", fgColor="E2EFDA")
        bold_font = Font(bold=True)
        for offset in range(1, len(DETAIL_LABELS) + 1):
            ws.cell(header_row + offset, _COL_LABEL).font = bold_font
        row = step_row + 2


def _category_sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, int):
        return (0, value)
    text = str(value).strip()
    if text.isdigit():
        return (0, int(text))
    return (1, text)


def build_workbook(items: list[dict[str, Any]], *,
                   summary_sheet: str = SUMMARY_SHEET) -> Workbook:
    """Rebuild a ``4.TestRequirement`` workbook from editor item dicts."""
    wb = Workbook()
    wb.remove(wb.active)
    summary = wb.create_sheet(summary_sheet)
    _write_summary(summary, items)
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for item in items:
        category = item.get("category")
        if category in (None, ""):
            continue
        grouped.setdefault(category, []).append(item)
    for category in sorted(grouped, key=_category_sort_key):
        _write_detail_sheet(wb.create_sheet(str(category)), grouped[category])
    return wb


def _drop_broken_defined_names(wb: Workbook, removed_titles: set[str]) -> None:
    """Remove workbook names that would point at deleted dynamic sheets.

    The original books carry Excel Table / print-area names such as
    ``DB[...]``. Keeping those while replacing the summary sheet makes Excel
    report a damaged workbook, so stale names are dropped before saving.
    """
    try:
        names = list(wb.defined_names.keys())
    except (AttributeError, TypeError):
        return
    for name in names:
        try:
            definition = wb.defined_names[name]
        except KeyError:
            continue
        text = (getattr(definition, "attr_text", None)
                or getattr(definition, "value", None) or "")
        if "DB[" in str(text) or any(title in str(text) for title in removed_titles):
            try:
                del wb.defined_names[name]
            except KeyError:
                pass


def rebuild_workbook(source: Union[str, BinaryIO],
                     items: list[dict[str, Any]], *,
                     summary_sheet: str = SUMMARY_SHEET) -> Workbook:
    """Write current rows into a preserved original workbook.

    Only the summary and its following numeric detail sheets are dynamic. The
    cover, introduction, strategy, overview and history sheets keep their
    original content, styles and order so the exported file can be edited and
    imported again as the same specification book.
    """
    wb = load_workbook(_as_seekable(source), data_only=False, read_only=False)
    summary = (wb[summary_sheet] if summary_sheet in wb.sheetnames
               else next((wb[name] for name in wb.sheetnames
                          if name.endswith("TestRequirement")), None))
    if summary is None:
        raise BookExcelError(f"原书中未找到设定 sheet「{summary_sheet}」")

    active_title = wb.active.title if wb.active is not None else None
    summary_index = wb.sheetnames.index(summary.title)
    removed_titles = {summary.title}
    wb.remove(summary)
    cursor = summary_index
    while cursor < len(wb.sheetnames) and wb.sheetnames[cursor].isdigit():
        removed_titles.add(wb.sheetnames[cursor])
        wb.remove(wb[wb.sheetnames[cursor]])

    grouped: dict[Any, list[dict[str, Any]]] = {}
    for item in items:
        category = item.get("category")
        if category in (None, ""):
            continue
        grouped.setdefault(category, []).append(item)

    new_summary = wb.create_sheet(summary.title, index=summary_index)
    _write_summary(new_summary, items)
    for offset, category in enumerate(sorted(grouped, key=_category_sort_key), start=1):
        detail = wb.create_sheet(str(category), index=summary_index + offset)
        _write_detail_sheet(detail, grouped[category])

    _drop_broken_defined_names(wb, removed_titles)
    if active_title in wb.sheetnames:
        wb.active = wb.sheetnames.index(active_title)
    return wb


def workbook_bytes(wb: Workbook) -> io.BytesIO:
    handle = io.BytesIO()
    wb.save(handle)
    handle.seek(0)
    return handle
