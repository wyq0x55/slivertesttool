"""Validate the typed Host oracle artifact before creating another candidate."""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any

from . import increment


def _table(data: bytes) -> tuple[increment.Table, list[list[str]]]:
    text = data.decode("cp932")
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ValueError("Malformed candidate CSV") from exc
    table = increment.parse(text)
    rows = [row for row in records if row and row[0] == ""]
    if rows != table.rows:
        raise ValueError("Malformed candidate data rows")
    metadata = [row for row in records if row and row[0] != ""]
    return table, metadata


def read_typed_candidate(execution: dict[str, Any], source: bytes, reference: bytes,
                         execution_root: Path) -> tuple[bytes, dict[str, Any]]:
    """Only blank added outputs may change, and rows may take the generator's
    labels and order; every identity is mandatory."""
    receipt = execution.get("typed_candidate")
    if (execution.get("status") != "HOST_PASS" or execution.get("evidence_source") != "HOST_EXECUTION"
            or execution.get("counts_as_target_evidence") is not False
            or not isinstance(receipt, dict)
            or receipt.get("contract") != "ut-agent.host-typed-candidate/1"
            or receipt.get("expected_value_source") != "IMPLEMENTATION_DERIVED"
            or receipt.get("source_candidate_sha256") != hashlib.sha256(source).hexdigest()):
        raise ValueError("Typed candidate contract/source identity does not match")
    sandbox_value, artifact_value = execution.get("sandbox"), receipt.get("path")
    if not isinstance(sandbox_value, str) or not isinstance(artifact_value, str):
        raise TypeError("Typed candidate sandbox/path is missing")
    sandbox, artifact = Path(sandbox_value), Path(artifact_value)
    if (not sandbox.is_absolute() or not artifact.is_absolute() or artifact.is_symlink()
            or not sandbox.resolve().is_relative_to(execution_root.resolve())
            or not artifact.resolve().is_relative_to(sandbox.resolve()) or not artifact.is_file()):
        raise ValueError("Typed candidate path escapes its execution sandbox")
    data = artifact.read_bytes()
    if hashlib.sha256(data).hexdigest() != receipt.get("sha256"):
        raise ValueError("Typed candidate artifact SHA does not match")
    before, _ = _table(source)
    after, _ = _table(data)
    original, _ = _table(reference)
    count = receipt.get("reference_rows")
    if (type(count) is not int or count != len(original.rows) or count < 0
            or count >= len(before.rows) or len(before.rows) != len(after.rows)
            or before.inputs != after.inputs or before.outputs != after.outputs
            or before.inputs != original.inputs or before.outputs != original.outputs
            or _header(before) != _header(after)):
        raise ValueError("Typed candidate changed columns, reference rows or row count")
    # ut-agent writes the typed candidate in the generator's form: added rows
    # are relabelled and placed among the reference rows, which stay unchanged
    # and in order.
    added_before, added_after = _added(before.rows, original.rows), _added(after.rows, original.rows)
    if added_before is None or added_after is None:
        raise ValueError("Typed candidate changed columns, reference rows or row count")
    ni, changed = len(before.inputs), False
    for old, new in zip(sorted(added_before), sorted(added_after)):
        if old[:1 + ni] != new[:1 + ni]:
            raise ValueError("Typed candidate changed input rows")
        for previous, current in zip(old[1 + ni:], new[1 + ni:]):
            if previous == current:
                continue
            if previous.strip() or not current.strip():
                raise ValueError("Typed candidate changed an original/nonblank expected value")
            changed = True
    if not changed:
        raise ValueError("Typed candidate did not fill an appended output")
    return data, dict(receipt)


def _header(table: increment.Table) -> list[str]:
    """The mod, stub and column lines, through ``#COMMENT``."""
    return table.lines[:next(i for i, line in enumerate(table.lines) if line.startswith("#COMMENT")) + 1]


def _added(rows: list[list[str]], reference: list[list[str]]) -> list[list[str]] | None:
    """Rows outside an order-preserving match of every reference row, or None."""
    added, position = [], 0
    for row in rows:
        if position < len(reference) and row == reference[position]:
            position += 1
        else:
            added.append(row)
    return added if position == len(reference) else None