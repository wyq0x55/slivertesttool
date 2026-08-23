"""Sparse procedure expansion — model writes semantics, code does bookkeeping.

The model emits steps keyed by name (only what changes, nothing else)::

    {"no": 1, "purpose": "超速触发", "inputs": {"engine.veh_speed": "120"},
     "expecteds": {"engine.warn_flag": "1"}, "timing": "即時"}

This module deterministically expands that into the platform's stored,
position-aligned format (blank cell = unchanged), backfilling 表示名 from
the registry. Every ambiguity is reported, never guessed: a name that
resolves to zero or several signals is a validation error the retry loop
feeds back to the model.
"""

from __future__ import annotations

from typing import Any

from .registry import Registry

_SPARSE_STEP_KEYS = {"no", "purpose", "operation", "subroutine", "args",
                     "inputs", "expecteds", "timing"}


def expand_procedure(sparse: Any, registry: Registry,
                     *, allowed_missing: set[str] | None = None) -> tuple[dict, list[str]]:
    """Expand one sparse procedure document into the stored steps format.

    Returns ``(steps_doc, problems)``; ``problems`` non-empty means the
    expansion must not be used. Names listed in ``allowed_missing`` (the
    item's declared ``missing_variables``) are skipped silently: the signal
    is not in the registry yet, the sbs scenario will register it before the
    procedure can run.
    """
    problems: list[str] = []
    allowed_missing = allowed_missing or set()
    if not isinstance(sparse, dict):
        return {}, ["稀疏手顺必须是对象"]
    unknown_fields = set(sparse) - {"steps"}
    if unknown_fields:
        problems.append(f"稀疏手顺含未知字段：{sorted(unknown_fields)}")
    steps = sparse.get("steps")
    if not isinstance(steps, list) or not steps:
        return {}, problems + ["steps 必须是非空数组"]

    in_paths: list[str] = []
    exp_paths: list[str] = []
    resolved_steps: list[dict] = []
    seen_no: set[int] = set()

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"steps[{i}] 必须是对象")
            continue
        extra = set(step) - _SPARSE_STEP_KEYS
        if extra:
            problems.append(f"steps[{i}] 含未知字段：{sorted(extra)}")
        if not isinstance(step.get("no"), int):
            problems.append(f"steps[{i}].no 必须是整数")
        elif step["no"] in seen_no:
            problems.append(f"steps[{i}].no 重复：{step['no']}")
        else:
            seen_no.add(step["no"])

        for key, target in (("inputs", in_paths), ("expecteds", exp_paths)):
            raw = step.get(key)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                problems.append(f"steps[{i}].{key} 必须是名字键控对象")
                continue
            cells: dict[str, str] = {}
            for name, value in raw.items():
                try:
                    path = registry.resolve(str(name))
                except KeyError:
                    if str(name) in allowed_missing:
                        continue
                    problems.append(
                        f"steps[{i}].{key} 引用了未知信号：{name}")
                    continue
                except ValueError as exc:
                    problems.append(f"steps[{i}].{key} {exc}")
                    continue
                if path not in target:
                    target.append(path)
                cells[path] = "" if value is None else str(value)
            step[f"_resolved_{key}"] = cells
        resolved_steps.append(step)

    if problems:
        return {}, problems

    def _aligned(key: str, paths: list[str]) -> list[list[str]]:
        rows = []
        for step in resolved_steps:
            cells = step.get(f"_resolved_{key}", {})
            rows.append([cells.get(p, "") for p in paths])
        return rows

    doc = {
        "input_signals": [[registry.display(p), p] for p in in_paths],
        "expected_signals": [[registry.display(p), p] for p in exp_paths],
        "steps": [],
    }
    in_rows = _aligned("inputs", in_paths)
    exp_rows = _aligned("expecteds", exp_paths)
    for step, in_row, exp_row in zip(resolved_steps, in_rows, exp_rows):
        clean = {k: step[k] for k in
                 ("no", "purpose", "operation", "subroutine", "args", "timing")
                 if step.get(k) not in (None, "")}
        if any(c != "" for c in in_row):
            clean["inputs"] = in_row
        if any(c != "" for c in exp_row):
            clean["expecteds"] = exp_row
        doc["steps"].append(clean)
    if not doc["input_signals"] and not doc["expected_signals"]:
        problems.append("手顺没有引用任何信号")
        return {}, problems
    return doc, []


def referenced_paths(steps_doc: dict) -> set[str]:
    """Paths actually used by an (expanded) steps document."""
    paths: set[str] = set()
    for group in ("input_signals", "expected_signals"):
        for pair in steps_doc.get(group) or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                paths.add(str(pair[1]))
    return paths
