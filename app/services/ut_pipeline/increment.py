"""Incremental input rows: the only change a model may make to a TestCsv.

A model never returns a CSV.  It names an obligation, a reference data row
and the input cells to change; the platform copies the reference row, applies
the changes and appends it with every expected-value cell blank.  Existing
lines are kept byte for byte, so rows cannot be deleted, columns cannot be
changed and expected values cannot be rewritten by construction.  Blank
expected values are then settled by the WinAMS capture.

Input columns are addressed by short tokens (``I1`` .. ``In``) so the prompt
stays small and a model cannot misspell a long WinAMS column name.  Reference
row 0 is a row with every input blank, for a CSV without data rows.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

MAX_ROWS_PER_ROUND = 20
# Rows added by a model are labelled in the CSV so a reviewer sees them.
LABEL_PREFIX = ";$L$,LLM add "
_UNSAFE = set(',"\r\n')


class TableError(ValueError):
    """The CSV is not a TestCsv the platform can extend."""


@dataclass(frozen=True)
class Table:
    lines: list[str]
    inputs: list[str]
    outputs: list[str]
    rows: list[list[str]]

    def token(self, index: int) -> str:
        return f"I{index + 1}"


def parse(text: str) -> Table:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
    try:
        records = [next(csv.reader([line])) if line else [] for line in lines]
        ni, no = int(records[0][3]), int(records[0][4])
    except (csv.Error, IndexError, ValueError, StopIteration) as exc:
        raise TableError(f"mod 行无法解析：{exc}") from exc
    header = next((row for row in records if row and row[0] == "#COMMENT"), None)
    if records[0][0] != "mod" or header is None or len(header) != 1 + ni + no:
        raise TableError("缺少 mod 行或 #COMMENT 列数与 mod 声明不一致")
    rows = [row for row in records if row and row[0] == "" and len(row) == 1 + ni + no]
    return Table(lines, header[1:1 + ni], header[1 + ni:], rows)


def _inputs(table: Table, row: list[str]) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in row[1:1 + len(table.inputs)])


def defaults(table: Table) -> list[str]:
    """Most common value per input column (ties: first seen)."""
    result = []
    for index in range(len(table.inputs)):
        counts = Counter(row[1 + index].strip() for row in table.rows)
        result.append(counts.most_common(1)[0][0] if counts else "")
    return result


def columns_section(table: Table, domains: dict[str, Any] | None = None) -> str:
    """Input columns with their tokens; fixed for a job, so it caches."""
    columns = (domains or {}).get("columns") or []
    def describe(index, name):
        suffix = ""
        if index < len(columns) and columns[index].get("column") == name:
            domain = columns[index]
            if domain.get("status") == "PROVEN":
                suffix = f" [范围 {domain['min']}..{domain['max']}]"
        return f"{table.token(index)} = {name}{suffix}"
    return "\n".join(describe(i, name) for i, name in enumerate(table.inputs))


def rows_section(table: Table) -> str:
    """Each data row as its differences from the per-column default."""
    base = defaults(table)
    unusual = " ".join(f"{table.token(i)}={value or '(空)'}"
                       for i, value in enumerate(base) if value != "0")
    if not table.rows:
        return "无数据行；用 base_row 0（全部输入为空的行）作参考行。"
    lines = [f"未列出的输入取默认值；默认值除下列外均为 0：{unusual or '无'}"]
    for number, row in enumerate(table.rows, start=1):
        cells = " ".join(f"{table.token(i)}={value or '(空)'}"
                         for i, value in enumerate(_inputs(table, row)) if value != base[i])
        lines.append(f"#{number}: {cells or '（全部默认）'}")
    return "\n".join(lines)


@dataclass
class Applied:
    csv_bytes: bytes | None
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def _column_index(table: Table, key: Any) -> int:
    key = str(key).strip()
    upper = key.upper()
    if upper.startswith("I") and upper[1:].isdigit():
        index = int(upper[1:]) - 1
        if 0 <= index < len(table.inputs):
            return index
    if key in table.inputs:
        return table.inputs.index(key)
    raise ValueError(f"未知输入列 {key}（只能改输入列，用 I1..I{len(table.inputs)}）")


def apply(table: Table, proposals: Any, allowed: set[str],
          domains: dict[str, Any] | None = None) -> Applied:
    """Validate proposals one by one and append the accepted rows.

    A rejected proposal never blocks the others; it is reported back so the
    next round and the reviewer can see why.
    """
    result = Applied(None)
    if not isinstance(proposals, list):
        result.rejected.append({"error": "rows 必须是数组"})
        return result
    seen = Counter(_inputs(table, row) for row in table.rows)
    ni, no = len(table.inputs), len(table.outputs)
    appended: list[str] = []
    for position, item in enumerate(proposals):
        try:
            if len(result.accepted) >= MAX_ROWS_PER_ROUND:
                raise ValueError(f"每轮最多新增 {MAX_ROWS_PER_ROUND} 行")
            if not isinstance(item, dict):
                raise ValueError("每个新增行必须是对象")
            oid = str(item.get("obligation") or "").strip()
            if oid not in allowed:
                raise ValueError(f"义务 {oid or '(空)'} 不在本轮可补的缺口中")
            try:
                base_number = int(item.get("base_row"))
            except (TypeError, ValueError):
                raise ValueError("base_row 必须是现有数据行编号") from None
            if not 0 <= base_number <= len(table.rows):
                raise ValueError(f"base_row {base_number} 超出 0..{len(table.rows)}")
            changes = item.get("inputs")
            if not isinstance(changes, dict) or not changes:
                raise ValueError("inputs 至少改动一个输入列")
            base = table.rows[base_number - 1] if base_number else [""] * (1 + ni)
            row = list(base[:1 + ni])
            changed = {}
            for key, value in changes.items():
                index = _column_index(table, key)
                text = str(value).strip() if value is not None else ""
                if not text or _UNSAFE & set(text):
                    raise ValueError(f"{table.token(index)} 的取值必须非空且不含逗号、引号或换行")
                row[1 + index] = text
                changed[table.token(index)] = text
            vector = _inputs(table, row)
            _check_domains(table, row, domains)
            if vector == _inputs(table, base):
                raise ValueError("改动后与参考行完全相同")
            if seen[vector]:
                raise ValueError("与已有数据行的输入完全相同，不能覆盖新的义务")
            seen[vector] += 1
            line = io.StringIO()
            csv.writer(line, lineterminator="").writerow(row + [""] * no)
            appended += [LABEL_PREFIX + oid, line.getvalue()]
            result.accepted.append({
                "obligation": oid, "base_row": base_number, "inputs": changed,
                "row": len(table.rows) + len(result.accepted) + 1,
                "reason": str(item.get("reason") or "")[:500],
            })
        except ValueError as exc:
            result.rejected.append({"index": position, "error": str(exc)})
    if appended:
        try:
            result.csv_bytes = ("\r\n".join(table.lines + appended) + "\r\n").encode("cp932")
        except UnicodeEncodeError as exc:
            result.rejected.append({"error": f"无法按 CP932 编码：{exc}"})
            result.accepted.clear()
    return result


def _check_domains(table: Table, row: list[str], domains: dict[str, Any] | None) -> None:
    """Enforce the generator's numeric bounds before accepting a proposal."""
    if not domains:
        return
    columns = domains.get("columns") or []
    if (domains.get("contract") != "ut-agent.input-domains/1"
            or len(columns) != len(table.inputs)
            or any(c.get("index") != i or c.get("column") != name
                   for i, (c, name) in enumerate(zip(columns, table.inputs), 1))):
        raise ValueError("输入类型契约与列不一致，需人工检查")
    for column, cell in zip(columns, row[1:]):
        text = cell.strip()
        if not text:
            continue
        if column.get("status") == "NEEDS_REVIEW":
            raise ValueError(f"输入类型证据冲突：{column['column']}")
        if column.get("status") != "PROVEN":
            continue
        if not re.fullmatch(r"[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
            raise ValueError(f"输入值无法按已证明的整数类型检查：{column['column']}={text}")
        value = int(text, 16 if text.lstrip("+-").lower().startswith("0x") else 10)
        if not column["min"] <= value <= column["max"]:
            raise ValueError(
                f"INPUT_OUT_OF_RANGE {column['column']}={text}；"
                f"允许 {column['min']}..{column['max']}，不截断；来源 {column.get('source', '')}")
