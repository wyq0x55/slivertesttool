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
from copy import copy
from pathlib import Path
from typing import Any, BinaryIO, Optional, Union
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ERROR_CODES, MergedCell
from openpyxl.formula.translate import Translator
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.utils.datetime import to_excel
from openpyxl.workbook.defined_name import DefinedName

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
_COL_SIGNAL_START = _mx.COL_SIGNAL_START


class BookExcelError(Exception):
    """User-facing error for a specification book workbook."""


def _as_seekable(source: Union[str, BinaryIO]) -> Union[str, io.BytesIO]:
    if isinstance(source, (str, Path)):
        return str(source)
    if isinstance(source, io.BytesIO):
        source.seek(0)
        return source

    # Werkzeug upload streams are ``SpooledTemporaryFile`` objects. On CPython
    # 3.10 they have ``seek`` but not ``seekable``, which openpyxl's zipfile
    # reader needs, so buffer those into memory before parsing.
    seekable = False
    try:
        seekable = bool(source.seekable())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - missing/broken seekable()
        seekable = False
    if seekable:
        try:
            source.seek(0)  # type: ignore[attr-defined]
            return source
        except Exception:  # noqa: BLE001
            pass
    try:
        source.seek(0)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
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
    if not isinstance(value, str) or not value.strip():
        return False
    if value.strip() in _DETAIL_LABEL_NAMES:
        return False
    # The header is immediately followed by the first metadata label. Step rows
    # never satisfy this. Some of the source blocks leave the item id blank.
    return _text(ws.cell(row + 1, _COL_LABEL).value).strip() == DETAIL_LABELS[0][0]


def _detail_blocks(ws) -> list[tuple[str, int, int]]:
    end = ws.max_row + 1
    headers = [row for row in range(1, end - 1) if _is_block_header(ws, row)]
    return [(_text(ws.cell(row, _COL_TEST_ID).value).strip(), row, limit)
            for row, limit in zip(headers, headers[1:] + [end])]


def _step_header(ws, row: int, limit: int) -> Optional[int]:
    return next((candidate for candidate in range(row + 1, min(row + 41, limit))
                 if ws.cell(candidate, _COL_STEP_NO).value == "手順番号"), None)


def _parse_detail_sheet(ws, out: dict[str, dict[str, Any]]) -> None:
    max_column = ws.max_column
    for test_id, row, limit in _detail_blocks(ws):
        step_header = _step_header(ws, row, limit)
        payload: dict[str, Any] = {
            "input_signals": [], "expected_signals": [], "steps": [],
        }
        if step_header is not None:
            input_signals, expected_signals, steps = _mx.parse_step_table(
                ws, step_header, end_row=limit - 1, max_column=max_column,
            )
            payload = {
                "input_signals": input_signals,
                "expected_signals": expected_signals,
                "steps": steps,
            }
        out[test_id] = payload


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


def _step_cells(steps: dict, header: int):
    inputs = steps.get("input_signals") or []
    expecteds = steps.get("expected_signals") or []
    signal_headers = ["入力値"] * len(inputs) + ["期待値"] * len(expecteds)
    headers = ["手順番号", "手順目的", "操作手順", "サブルーチン", "引数",
               *signal_headers, "確認タイミング"]
    for column, label in enumerate(headers, start=_COL_STEP_NO):
        yield (header, column), label
    for column, pair in enumerate([*inputs, *expecteds], start=_COL_SIGNAL_START):
        for index in range(2):
            yield (header + index + 1, column), pair[index] if index < len(pair) else None
    for row, step in enumerate(steps.get("steps") or [], start=header + 3):
        values = [step.get(key) for key in ("no", "purpose", "operation", "subroutine", "args")]
        for key, signals in (("inputs", inputs), ("expecteds", expecteds)):
            entries = step.get(key) or []
            values.extend(entries[index] if index < len(entries) else None
                          for index in range(len(signals)))
        values.append(step.get("timing"))
        for column, value in enumerate(values, start=_COL_STEP_NO):
            yield (row, column), value


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
        row = _write_detail_block(ws, item, row)


def _write_detail_block(ws, item: dict, row: int) -> int:
    steps = _steps_doc(item.get("steps"))
    if not any(steps.get(key) for key in ("steps", "input_signals", "expected_signals")):
        return row
    ws.cell(row, 1).value = item.get("sheet_name") or item.get("test_id")
    header = row + 2
    ws.cell(header, _COL_TEST_ID).value = item.get("test_id")
    ws.cell(header, _COL_ITEM_ID).value = item.get("item_id")
    for offset, (label, key) in enumerate(DETAIL_LABELS, start=1):
        ws.cell(header + offset, _COL_LABEL, label).font = Font(bold=True)
        ws.cell(header + offset, 3).value = ": " + _text(_display(item.get(key)))
    step_header = header + len(DETAIL_LABELS) + 2
    for (cell_row, column), value in _step_cells(steps, step_header):
        cell = ws.cell(cell_row, column, value)
        if cell_row == step_header:
            cell.fill = PatternFill("solid", fgColor="E2EFDA")
    ws.cell(step_header, _COL_STEP_NO).font = Font(bold=True)
    return step_header + 5 + len(steps.get("steps") or [])


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


def _same_value(first, second) -> bool:
    return _text(_cell_or_none(first)) == _text(_cell_or_none(second))


def _formula_conflict(cell) -> BookExcelError:
    return BookExcelError(
        f"无法导出「{cell.parent.title}」!{cell.coordinate}：修改值与原有公式不一致。"
        "请修改公式引用的源字段，或在 Excel 中调整原公式后重新导入。"
    )


def _write_value(cell, value, cache) -> None:
    if cell.data_type == "f":
        if not _same_value(cache.get(cell.coordinate), value):
            raise _formula_conflict(cell)
        return
    if not _same_value(cell.value, value):
        cell.value = value
        if isinstance(value, str):
            cell.data_type = "s"
        cache.pop(cell.coordinate, None)


def _translated_formula(formula, origin: str, destination: str):
    if origin == destination:
        return formula
    return Translator(formula, origin=origin).translate_formula(destination)


def _write_summary_formula(key, cell, value, item, previous, cached, header_row, columns, source_row, cache):
    """Preserve imported category and test numbers when row order breaks their sequence."""
    original = cache.get(cell.coordinate)
    row = cell.row
    prior = row - 1
    item_id_header = (_text(cached.cell(header_row, columns["item_id"]).value).strip()
                      if "item_id" in columns else SUMMARY_HEADERS[5]) or SUMMARY_HEADERS[5]
    if key == "test_id" and cell.value == f'="QTS-"&DB[[#This Row],[{item_id_header}]]':
        if not _same_value(item.get("item_id"), cached.cell(source_row, columns["item_id"]).value):
            if value != "QTS-" + _text(item.get("item_id")):
                raise _formula_conflict(cell)
            cache[cell.coordinate] = value
            return
    elif key in ("category", "test_no"):
        old_prior_id = cached.cell(source_row - 1, columns["test_id"]).value
        if old_prior_id == SUMMARY_HEADERS[0]:
            old_prior_id = None
        moved = _text(old_prior_id).strip() != _text(previous.get("test_id")).strip()
        previous_category = previous.get("category")
        category_column = get_column_letter(columns["category"])
        sheet_column = get_column_letter(columns["sheet_name"])
        number_column = get_column_letter(columns["test_no"])
        category_now, category_before = f"{category_column}{row}", f"{category_column}{prior}"
        category_formula = (
            f"=IFERROR(IF({sheet_column}{row}<>{sheet_column}{prior},"
            f"{category_before}+1,{category_before}),1)")
        number_formula = (
            f"=IF({category_now}<>{category_before},1,IFERROR({number_column}{prior}+1,1))")
        if key == "category" and cell.value == category_formula:
            current_name = _text(item.get("sheet_name")).strip().casefold()
            previous_name = _text(previous.get("sheet_name")).strip().casefold()
            expected = (previous_category + (current_name != previous_name)
                        if isinstance(previous_category, int) else 1)
            dependencies = ((item, source_row, "sheet_name"),
                            (previous, source_row - 1, "sheet_name"),
                            (previous, source_row - 1, "category"))
        elif key == "test_no" and cell.value == number_formula:
            previous_number = previous.get("test_no")
            expected = (previous_number + 1
                        if _to_int(item.get("category")) == previous_category
                        and isinstance(previous_number, int) else 1)
            dependencies = ((item, source_row, "category"),
                            (previous, source_row - 1, "category"),
                            (previous, source_row - 1, "test_no"))
        else:
            _write_value(cell, value, cache)
            return
        inputs_changed = moved or any(
            not _same_value(values.get(field), cached.cell(old_row, columns[field]).value)
            for values, old_row, field in dependencies
            if values
        )
        if inputs_changed or _to_int(original) != value:
            if expected == value:
                cache[cell.coordinate] = expected
                return
            if moved and _to_int(original) == value:
                cell.value = value
                cache.pop(cell.coordinate, None)
                return
            raise _formula_conflict(cell)
        return
    _write_value(cell, value, cache)


def _update_summary(ws, cached, items, cache) -> tuple[dict, dict[str, str], dict]:
    from .testmatrix_bridge import PRIORITY_EN_TO_JP, normalize_priority

    header, columns = _detect_header(cached)
    last_column = ws.max_column
    data_rows = {
        test_id: row
        for row in range(header + 1, cached.max_row + 1)
        if (test_id := _text(cached.cell(row, columns["test_id"]).value).strip())
    }
    source_rows = {
        test_id: [(cell.coordinate, cell.value, cell.data_type, cell._style,
                   cache.get(cell.coordinate))
                  for cell in ws[row][:last_column]]
        for test_id, row in data_rows.items()
    }
    for row in data_rows.values():
        for cell in ws[row][:last_column]:
            if not isinstance(cell, MergedCell):
                cell.value = None
                cache.pop(cell.coordinate, None)
    exported = {}
    changes = {}
    previous = {}
    for row, item in enumerate(items, start=header + 1):
        item = dict(item)
        test_id = item.get("test_id")
        source_row = data_rows.get(test_id)
        source = source_rows.get(test_id, ())
        changed_fields = {}
        for column, (origin, value, data_type, style, cached_value) in enumerate(source, 1):
            cell = ws.cell(row, column)
            if isinstance(cell, MergedCell):
                continue
            if data_type == "f":
                cell.value = _translated_formula(value, origin, cell.coordinate)
                cache[cell.coordinate] = cached_value
            else:
                cell.value = value
                cell.data_type = data_type
            cell._style = style
        for key, column in columns.items():
            value = item.get(key)
            cell = ws.cell(row, column)
            original = cache.get(cell.coordinate) if cell.data_type == "f" else cell.value
            if key in ("category", "test_no"):
                value = _to_int(value)
            if key == "priority" and value in PRIORITY_EN_TO_JP:
                value = (original if normalize_priority(original) == value
                         else PRIORITY_EN_TO_JP[value])
            if key == "result":
                value = _export_result(value, original)
            if key == "exec_date" and isinstance(value, str):
                try:
                    value = _dt.datetime.fromisoformat(value)
                except ValueError:
                    pass
            if cell.data_type == "f":
                _write_summary_formula(
                    key, cell, value, item, previous, cached, header, columns, source_row or row, cache)
            else:
                _write_value(cell, value, cache)
            actual = cache.get(cell.coordinate) if cell.data_type == "f" else cell.value
            if not _same_value(actual, original):
                changed_fields[key] = (column, actual)
            item[key] = value
        exported[test_id] = item
        changes[test_id] = changed_fields
        previous = item
    for table in ws.tables.values():
        left, top, right, original_bottom = range_boundaries(table.ref)
        data_bottom = header + max(len(items), 1)
        # Keep template padding rows while the row count is unchanged; resize only
        # when items are added or removed.
        bottom = (max(original_bottom, data_bottom)
                  if len(items) == len(data_rows) else data_bottom)
        if top != header or not left <= columns["test_id"] <= right:
            continue
        table.ref = f"{get_column_letter(left)}{top}:{get_column_letter(right)}{bottom}"
        if table.autoFilter is not None:
            table.autoFilter.ref = table.ref
    categories = {
        test_id: _text(_to_int(cached.cell(row, columns["category"]).value)).strip()
        for test_id, row in data_rows.items()
    }
    return exported, categories, changes


def normalize_result(value: Any) -> str:
    from .testmatrix_bridge import RESULT_JP_TO_EN

    token = _text(value).strip()
    return {"Passed": "Pass", "Failed": "Fail"}.get(
        token, RESULT_JP_TO_EN.get(token, token))


def _export_result(value, original):
    outcome = normalize_result(value)
    if outcome == normalize_result(original):
        return original
    return {"Pass": "Passed", "Fail": "Failed", "Not Tested": "-"}.get(outcome, value)


def _detail_step_updates(cached, header, step_header, limit, item, max_column):
    steps = _steps_doc(item.get("steps"))
    if step_header is None and not any(steps.values()):
        return header + len(DETAIL_LABELS), {}, set()
    old_cells = {}
    old_end = header + len(DETAIL_LABELS)
    if step_header is not None:
        inputs, expecteds, old_steps = _mx.parse_step_table(
            cached, step_header, end_row=limit - 1, max_column=max_column)
        old_end = step_header + 2 + len(old_steps)
        old_cells = dict(_step_cells({
            "input_signals": inputs, "expected_signals": expecteds, "steps": old_steps,
        }, step_header))
        if (inputs, expecteds, old_steps) == tuple(
                steps.get(key) or [] for key in ("input_signals", "expected_signals", "steps")):
            return old_end, {}, set()
    else:
        step_header = old_end + 2
    new_cells = dict(_step_cells(steps, step_header)) if any(steps.values()) else {}
    updates = {coord: value for coord, value in new_cells.items()
               if coord not in old_cells or not _same_value(old_cells[coord], value)}
    return old_end, updates, old_cells.keys() - new_cells.keys()


def _write_detail_formula(cell, field, source, header, lookup_columns, lookup_id, cache, epoch):
    column, value = source
    name = lookup_columns.get(column)
    key_cell = cell.parent.cell(header, _COL_TEST_ID)
    key = cache.get(key_cell.coordinate) if key_cell.data_type == "f" else key_cell.value
    if key != lookup_id or any(char in lookup_id for char in "*?~"):
        raise _formula_conflict(cell)
    lookups = (
        f'VLOOKUP(B{header},DB[],COLUMN(DB[{name}]),0)',
        f'VLOOKUP(B{header},DB[#Data],COLUMN(DB[{name}]),0)',
    )
    formulas = tuple(
        f'=": "&{lookup}' if field != "item_id" else f"={lookup}"
        for lookup in lookups
    )
    date_formula = field == "exec_date" and cell.value in tuple(
        f'=": "&TEXT({lookup},"yyyy/mm/dd")' for lookup in lookups
    )
    if name is None or (cell.value not in formulas and not date_formula):
        raise _formula_conflict(cell)
    if date_formula:
        if isinstance(value, _dt.date):
            cache[cell.coordinate] = ": " + value.strftime("%Y/%m/%d")
        else:
            cache.pop(cell.coordinate, None)
        return
    if isinstance(value, (_dt.date, _dt.time, _dt.timedelta)):
        value = to_excel(value, epoch)
    if value is None:
        value = 0
    if field != "item_id":
        if isinstance(value, bool):
            value = "TRUE" if value else "FALSE"
        if isinstance(value, float):
            value = format(value, ".15g")
        value = ": " + str(value)
    cache[cell.coordinate] = value


def _update_details(wb, cached_wb, summary, items, original_categories, changes, caches) -> None:
    by_sheet = {source.title: _detail_blocks(source)
                for source in cached_wb.worksheets if source.title.isdigit()}
    imported_blocks = {test_id: (title, row) for title, blocks in by_sheet.items()
                       for test_id, row, _ in blocks}
    next_sheet = wb.index(summary) + 1 + sum(
        name.isdigit() for name in wb.sheetnames[wb.index(summary) + 1:])
    header_row, columns = _detect_header(summary)
    lookup_columns = {}
    table = summary.tables.get("DB")
    if table is not None:
        left, top, right, _bottom = range_boundaries(table.ref)
        if left == columns["test_id"] == 1 and top == header_row:
            lookup_columns = {column: table_column.name for column, table_column in
                              zip(range(left, right + 1), table.tableColumns)}
    first_lookup_ids = {test_id.casefold(): test_id for test_id in reversed(items)}
    for title, blocks in by_sheet.items():
        source = cached_wb[title]
        ws = wb[title]
        source_cache = caches[ws.title]
        last_column = ws.max_column
        end = source.max_row + 1
        for test_id, header, limit in blocks:
            item = items.get(test_id)
            # Only the last block for a duplicated test id is authoritative. Earlier
            # blocks are archived source and must keep their formulas untouched.
            if item is not None and imported_blocks[test_id] != (title, header):
                continue
            category = _text(_to_int(item.get("category"))).strip() if item else ""
            if item is None and test_id not in original_categories:
                continue
            original_sheet = imported_blocks[test_id][0]
            if item is not None and category == original_categories.get(test_id):
                category = original_sheet
            step_header = _step_header(source, header, limit)
            old_end, updates, removed_cells = _detail_step_updates(
                source, header, step_header, limit, item or {}, last_column)
            metadata_cells = {}
            if category.isdigit():
                for row in range(header + 1, step_header or min(limit, header + 14)):
                    label = source.cell(row, _COL_LABEL).value
                    for expected_label, key in DETAIL_LABELS:
                        if label == expected_label:
                            value = item.get(key)
                            if key == "exec_date" and isinstance(value, (_dt.datetime, _dt.date)):
                                value = value.strftime("%Y/%m/%d")
                            metadata_cells[row, 3] = (key, ": " + _text(value))
                            break
                metadata_cells[header, _COL_ITEM_ID] = ("item_id", item.get("item_id"))
            target = ws
            offset = 0
            new_end = max((row for row, _ in updates), default=old_end)
            relocated = category.isdigit() and (
                category != ws.title or (new_end > old_end and limit != end and new_end >= limit - 2)
            )
            if relocated:
                if category not in wb.sheetnames:
                    wb.create_sheet(category, index=next_sheet)
                    next_sheet += 1
                target = wb[category]
                offset = target.max_row + 3 - header
                target_cache = caches.setdefault(category, {})
                for row in ws.iter_rows(min_row=header, max_row=old_end, max_col=last_column):
                    for cell in row:
                        if isinstance(cell, MergedCell):
                            continue
                        dest = target.cell(cell.row + offset, cell.column)
                        dest._style = copy(cell._style)
                        if cell.data_type == "f":
                            dest.value = _translated_formula(cell.value, cell.coordinate,
                                                             dest.coordinate)
                            target_cache[dest.coordinate] = source_cache.get(cell.coordinate)
                        else:
                            dest.value = cell.value
                            dest.data_type = cell.data_type
                target.cell(header + offset - 2, 1).value = item.get("sheet_name") or test_id
            if relocated or not category.isdigit():
                for row in ws.iter_rows(min_row=header, max_row=old_end, max_col=last_column):
                    for cell in row:
                        if not isinstance(cell, MergedCell):
                            cell.value = None
                            source_cache.pop(cell.coordinate, None)
            if category.isdigit():
                target_cache = caches[target.title]
                for row, column in removed_cells:
                    cell = target.cell(row + offset, column)
                    cell.value = None
                    target_cache.pop(cell.coordinate, None)
                for (row, column), value in updates.items():
                    cell = target.cell(row + offset, column)
                    _write_value(cell, value, target_cache)
                for (row, column), (field, value) in metadata_cells.items():
                    cell = target.cell(row + offset, column)
                    if cell.data_type != "f":
                        _write_value(cell, value, target_cache)
                    elif field in changes[test_id]:
                        if first_lookup_ids[test_id.casefold()] != test_id:
                            raise _formula_conflict(cell)
                        _write_detail_formula(cell, field, changes[test_id][field], header + offset,
                                              lookup_columns, test_id, target_cache, wb.epoch)
    for test_id, item in items.items():
        if test_id in imported_blocks:
            continue
        category = _text(_to_int(item.get("category"))).strip()
        if category.isdigit():
            if category not in wb.sheetnames:
                wb.create_sheet(category, index=next_sheet)
                next_sheet += 1
            ws = wb[category]
            _write_detail_block(ws, item, ws.max_row + 2)


def _restore_table_print_areas(wb: Workbook, source) -> None:
    """Keep structured print-area formulas that the worksheet reader drops."""
    with ZipFile(_as_seekable(source)) as original:
        document = ElementTree.fromstring(original.read("xl/workbook.xml"))
    for name in document.findall("./{*}definedNames/{*}definedName"):
        value = name.text or ""
        index = name.get("localSheetId")
        if name.get("name") == "_xlnm.Print_Area" and index is not None and "DB[" in value:
            sheet = wb[wb.sheetnames[int(index)]]
            if not sheet.print_area:
                sheet.defined_names.add(DefinedName(name.get("name"), attr_text=value))


def _clear_nested_view_autofilters(wb: Workbook, source) -> None:
    """Drop sheet-level autoFilter hoisted by openpyxl from <customSheetView>.

    openpyxl's worksheet reader parses any <autoFilter> element, including one
    nested inside a personal <customSheetView>, into ``ws.auto_filter``. When a
    worksheet also hosts an Excel Table with its own autoFilter, re-saving that
    hoisted top-level <autoFilter> produces a workbook Microsoft Excel refuses
    to open. Clear ``ws.auto_filter`` whenever the original sheet XML had no
    direct top-level <autoFilter> child.
    """
    with ZipFile(_as_seekable(source)) as original:
        names = set(original.namelist())
        if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
            return
        workbook_xml = ElementTree.fromstring(original.read("xl/workbook.xml"))
        rels_xml = ElementTree.fromstring(original.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {}
        for rel in rels_xml.findall("./{*}Relationship"):
            rid = rel.get("Id")
            target = (rel.get("Target") or "").lstrip("/")
            if rid and target:
                rel_targets[rid] = target if target.startswith("xl/") else f"xl/{target}"
        rel_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for sheet_el in workbook_xml.findall("./{*}sheets/{*}sheet"):
            title = sheet_el.get("name")
            if not title or title not in wb.sheetnames:
                continue
            ws = wb[title]
            if not ws.auto_filter.ref:
                continue
            part = rel_targets.get(sheet_el.get(rel_attr) or "")
            if not part or part not in names:
                continue
            sheet_xml = ElementTree.fromstring(original.read(part))
            if sheet_xml.find("./{*}autoFilter") is None:
                ws.auto_filter.ref = None
                ws.auto_filter.filterColumn.clear()
                ws.auto_filter.sortState = None


def rebuild_workbook(source: Union[str, BinaryIO],
                     items: list[dict[str, Any]], *,
                     summary_sheet: str = SUMMARY_SHEET) -> io.BytesIO:
    """Update the original sheets and retain formula results for immediate import."""
    source = _as_seekable(source)
    wb = load_workbook(source, data_only=False)
    cached_wb = load_workbook(_as_seekable(source), data_only=True)
    summary = (wb[summary_sheet] if summary_sheet in wb.sheetnames
               else next((wb[name] for name in wb.sheetnames
                          if name.endswith("TestRequirement")), None))
    if summary is None:
        raise BookExcelError(f"原书中未找到设定 sheet「{summary_sheet}」")

    _restore_table_print_areas(wb, source)
    _clear_nested_view_autofilters(wb, source)
    active_sheet = wb.active
    caches = {}
    for ws in wb.worksheets:
        cached = cached_wb[ws.title]
        caches[ws.title] = {
            cell.coordinate: cached[cell.coordinate].value
            for row in ws.iter_rows() for cell in row if cell.data_type == "f"
        }
    exported, original_categories, changes = _update_summary(
        summary, cached_wb[summary.title], items, caches[summary.title])
    _update_details(wb, cached_wb, summary, exported, original_categories, changes, caches)
    if active_sheet is not None:
        wb.active = wb.index(active_sheet)
    wb.calculation.fullCalcOnLoad = True
    return workbook_bytes(wb, caches=caches)


def workbook_bytes(wb: Workbook, *, caches: Optional[dict] = None) -> io.BytesIO:
    handle = io.BytesIO()
    wb.save(handle)
    handle.seek(0)
    if not caches:
        return handle
    indexed = {
        f"xl/worksheets/sheet{index}.xml": caches[ws.title]
        for index, ws in enumerate(wb.worksheets, 1) if caches.get(ws.title)
    }
    output = io.BytesIO()
    with ZipFile(handle) as original, ZipFile(output, "w", ZIP_DEFLATED) as result:
        for name in original.namelist():
            data = original.read(name)
            if name in indexed:
                data = _with_formula_cache(data, indexed[name], wb.epoch)
            result.writestr(name, data)
    output.seek(0)
    return output


def _with_formula_cache(data: bytes, cache: dict, epoch) -> bytes:
    root = ElementTree.fromstring(data)
    namespace = root.tag.rsplit("}", 1)[0] + "}"
    for cell in root.iter(namespace + "c"):
        value = cache.get(cell.get("r"))
        if value is None or cell.find(namespace + "f") is None:
            continue
        node = cell.find(namespace + "v")
        if node is None:
            node = ElementTree.SubElement(cell, namespace + "v")
        if isinstance(value, bool):
            cell.set("t", "b")
            value = int(value)
        elif isinstance(value, str):
            cell.set("t", "e" if value in ERROR_CODES else "str")
        elif isinstance(value, (_dt.datetime, _dt.date, _dt.time, _dt.timedelta)):
            value = to_excel(value, epoch)
        node.text = str(value)
    return ElementTree.tostring(root, encoding="utf-8")
