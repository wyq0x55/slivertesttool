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


def read_presented_candidate(execution: dict[str, Any] | None, source: bytes) -> bytes | None:
    """The display form of an executed candidate, or None when it has none.

    ut-agent relabels and reorders witnessed appended rows with the generator's
    rules. It is accepted only as a permutation of the executed candidate's
    rows under the same header, so it never carries different test content.
    """
    receipt = (execution or {}).get("presented_candidate")
    if receipt is None:
        return None
    if (execution.get("status") != "HOST_PASS" or not isinstance(receipt, dict)
            or receipt.get("contract") != "ut-agent.host-presented-candidate/1"
            or receipt.get("source_candidate_sha256") != hashlib.sha256(source).hexdigest()):
        raise ValueError("Presented candidate contract/source identity does not match")
    sandbox_value, artifact_value = execution.get("sandbox"), receipt.get("path")
    if not isinstance(sandbox_value, str) or not isinstance(artifact_value, str):
        raise TypeError("Presented candidate sandbox/path is missing")
    sandbox, artifact = Path(sandbox_value), Path(artifact_value)
    if (not sandbox.is_absolute() or not artifact.is_absolute() or artifact.is_symlink()
            or not artifact.resolve().is_relative_to(sandbox.resolve()) or not artifact.is_file()):
        raise ValueError("Presented candidate path escapes its execution sandbox")
    data = artifact.read_bytes()
    if hashlib.sha256(data).hexdigest() != receipt.get("sha256"):
        raise ValueError("Presented candidate artifact SHA does not match")
    before, _ = _table(source)
    after, _ = _table(data)

    def header(table: increment.Table) -> list[str]:
        return table.lines[:next(i for i, line in enumerate(table.lines) if line.startswith("#COMMENT")) + 1]

    if header(before) != header(after) or sorted(before.rows) != sorted(after.rows):
        raise ValueError("Presented candidate changed the header or data rows")
    return data


def read_typed_candidate(execution: dict[str, Any], source: bytes, reference: bytes,
                         execution_root: Path) -> tuple[bytes, dict[str, Any]]:
    """Only blank appended outputs may change; every identity is mandatory."""
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
    before, metadata = _table(source)
    after, derived_metadata = _table(data)
    original, _ = _table(reference)
    count = receipt.get("reference_rows")
    if (type(count) is not int or count != len(original.rows) or count < 0
            or count >= len(before.rows) or len(before.rows) != len(after.rows)
            or before.inputs != after.inputs or before.outputs != after.outputs
            or before.inputs != original.inputs or before.outputs != original.outputs
            or metadata != derived_metadata
            or before.rows[:count] != original.rows):
        raise ValueError("Typed candidate changed columns, reference rows or row count")
    ni, changed = len(before.inputs), False
    for index, (old, new) in enumerate(zip(before.rows, after.rows)):
        if old[:1 + ni] != new[:1 + ni]:
            raise ValueError("Typed candidate changed input rows")
        for previous, current in zip(old[1 + ni:], new[1 + ni:]):
            if previous == current:
                continue
            if index < count or previous.strip() or not current.strip():
                raise ValueError("Typed candidate changed an original/nonblank expected value")
            changed = True
    if not changed:
        raise ValueError("Typed candidate did not fill an appended output")
    return data, dict(receipt)
