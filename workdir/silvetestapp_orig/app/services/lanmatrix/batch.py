"""Batch-edit operation engine (FR-BATCH-001..003). Pure and Flask-independent.

Given an operation spec and the current field value, :func:`apply_operation`
returns the new value. The service layer iterates matching records, runs this
to produce old/new pairs for the preview (FR-BATCH-002), then commits inside a
single transaction (FR-BATCH-003). Undo replays the stored old values.
"""

from __future__ import annotations

from typing import Any

from . import security

# Supported batch operations (FR-BATCH-001).
OPERATIONS: tuple[str, ...] = (
    "set",              # set a fixed value
    "clear",            # clear to empty
    "prefix",           # text prefix
    "suffix",           # text suffix
    "find_replace",     # literal find/replace
    "regex_replace",    # regex replace (bounded)
    "increment",        # numeric add
    "decrement",        # numeric subtract
    "status_transition",# set workflow/result state
    "multi_add",        # add option(s) to a multi-select
    "multi_remove",     # remove option(s) from a multi-select
)


class BatchOperationError(ValueError):
    """Raised when an operation spec is malformed."""


def validate_operation(op: dict[str, Any]) -> None:
    kind = op.get("op")
    if kind not in OPERATIONS:
        raise BatchOperationError(f"unknown batch operation: {kind}")
    if kind in ("set", "status_transition") and "value" not in op:
        raise BatchOperationError(f"'{kind}' requires 'value'")
    if kind in ("prefix", "suffix") and not op.get("value"):
        raise BatchOperationError(f"'{kind}' requires non-empty 'value'")
    if kind == "find_replace" and "find" not in op:
        raise BatchOperationError("find_replace requires 'find'")
    if kind == "regex_replace" and "pattern" not in op:
        raise BatchOperationError("regex_replace requires 'pattern'")
    if kind in ("increment", "decrement"):
        try:
            float(op.get("value", 0))
        except (TypeError, ValueError):
            raise BatchOperationError(f"'{kind}' requires numeric 'value'")
    if kind in ("multi_add", "multi_remove"):
        vals = op.get("value")
        if not isinstance(vals, (list, tuple)) or not vals:
            raise BatchOperationError(f"'{kind}' requires a non-empty list 'value'")


def apply_operation(op: dict[str, Any], current: Any) -> Any:
    """Return the new value produced by applying ``op`` to ``current``."""
    kind = op["op"]

    if kind == "set" or kind == "status_transition":
        return op["value"]

    if kind == "clear":
        return None

    if kind == "prefix":
        return f"{op['value']}{'' if current is None else current}"

    if kind == "suffix":
        return f"{'' if current is None else current}{op['value']}"

    if kind == "find_replace":
        if current is None:
            return None
        return str(current).replace(op["find"], op.get("replace", ""))

    if kind == "regex_replace":
        if current is None:
            return None
        compiled = security.compile_user_regex(op["pattern"])
        return compiled.sub(op.get("replace", ""), str(current))

    if kind in ("increment", "decrement"):
        base = _as_number(current)
        delta = _as_number(op["value"])
        result = base + delta if kind == "increment" else base - delta
        # Keep integers integral.
        if isinstance(base, int) and float(delta).is_integer():
            return int(result)
        return result

    if kind == "multi_add":
        vals = list(current) if isinstance(current, list) else []
        for v in op["value"]:
            if v not in vals:
                vals.append(v)
        return vals

    if kind == "multi_remove":
        vals = list(current) if isinstance(current, list) else []
        remove = set(op["value"])
        return [v for v in vals if v not in remove]

    raise BatchOperationError(f"unhandled operation: {kind}")


def _as_number(value: Any):
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise BatchOperationError("boolean is not numeric")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise BatchOperationError(f"'{value}' is not numeric")
    return int(f) if f.is_integer() else f
