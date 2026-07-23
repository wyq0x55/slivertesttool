"""Field validation engine (FR-GRID-005). Flask-independent and pure.

A :class:`FieldSpec` describes one field (system or custom): its data type,
required flag, options and a ``validation_rule`` dict. :func:`validate_value`
returns a list of :class:`FieldError` (empty == valid). :func:`validate_record`
validates a whole record against a set of specs and can enforce cross-field and
project-unique rules through injected callbacks.

Supported rules (``validation_rule`` keys):
    required, min_length, max_length, min, max, pattern, enum, unique,
    can_std_id, can_ext_id, timeout_min, timeout_max, no_control_chars,
    date_before (other field), date_after (other field).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import fields as fld
from . import security

# CAN identifier ranges (11-bit standard, 29-bit extended).
CAN_STD_MAX = 0x7FF
CAN_EXT_MAX = 0x1FFFFFFF


@dataclass
class FieldSpec:
    field_key: str
    data_type: str
    display_name: str = ""
    is_required: bool = False
    is_readonly: bool = False
    options: list[str] = field(default_factory=list)
    rule: dict[str, Any] = field(default_factory=dict)
    sheet: str = "test"

    @classmethod
    def from_definition(cls, d: dict[str, Any]) -> "FieldSpec":
        return cls(
            field_key=d["field_key"],
            data_type=d["data_type"],
            display_name=d.get("display_name", d["field_key"]),
            is_required=bool(d.get("is_required", False)),
            is_readonly=bool(d.get("is_readonly", False)),
            options=list(d.get("options") or []),
            rule=dict(d.get("validation_rule") or d.get("rule") or {}),
            sheet=d.get("sheet") or "test",
        )


@dataclass
class FieldError:
    field: str
    message: str
    severity: str = "blocking"  # blocking | warning

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message, "severity": self.severity}


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "") or (
        isinstance(value, (list, tuple)) and len(value) == 0
    )


def validate_value(
    spec: FieldSpec,
    raw: Any,
    *,
    unique_checker: Optional[Callable[[str, Any], bool]] = None,
    enforce_required: bool = True,
) -> tuple[Any, list[FieldError]]:
    """Coerce + validate a single value. Returns ``(coerced, errors)``.

    ``enforce_required=False`` skips the "required" check so a *draft* row can
    be created with blank cells and completed inline afterwards (the required
    constraint is then enforced on later edits / review).
    """
    errors: list[FieldError] = []
    rule = spec.rule
    required = spec.is_required or bool(rule.get("required"))

    if _is_empty(raw):
        if required and enforce_required:
            errors.append(FieldError(spec.field_key, f"{spec.display_name}为必填项"))
        return None, errors

    # Forbid control characters up-front (FR-GRID-005).
    if rule.get("no_control_chars", True) and security.has_control_chars(raw):
        errors.append(FieldError(spec.field_key, "包含非法控制字符"))

    try:
        value = fld.coerce_value(spec.data_type, raw)
    except fld.CoercionError as exc:
        errors.append(FieldError(spec.field_key, str(exc)))
        return None, errors

    errors.extend(_check_rules(spec, value, rule))

    # Enum / option membership.
    options = spec.options or rule.get("enum")
    if options:
        if spec.data_type == "multi_select" and isinstance(value, list):
            bad = [v for v in value if v not in options]
            if bad:
                errors.append(FieldError(spec.field_key, f"非法选项: {', '.join(bad)}"))
        elif spec.data_type in ("single_select",) and value not in options:
            errors.append(FieldError(spec.field_key, f"非法枚举值: {value}"))

    # Project-internal uniqueness.
    if rule.get("unique") and unique_checker is not None:
        if not unique_checker(spec.field_key, value):
            errors.append(FieldError(spec.field_key, "项目内已存在相同值"))

    return value, errors


def _check_rules(spec: FieldSpec, value: Any, rule: dict[str, Any]) -> list[FieldError]:
    out: list[FieldError] = []
    key, name = spec.field_key, spec.display_name

    if isinstance(value, str):
        mn, mx = rule.get("min_length"), rule.get("max_length")
        if mn is not None and len(value) < mn:
            out.append(FieldError(key, f"{name}长度不能小于 {mn}"))
        if mx is not None and len(value) > mx:
            out.append(FieldError(key, f"{name}长度不能超过 {mx}"))
        pat = rule.get("pattern")
        if pat:
            try:
                compiled = security.compile_user_regex(pat)
                if not compiled.fullmatch(value):
                    out.append(FieldError(key, f"{name}格式不正确"))
            except security.UnsafeRegexError as exc:
                out.append(FieldError(key, f"校验规则错误: {exc}"))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = rule.get("min"), rule.get("max")
        if lo is not None and value < lo:
            out.append(FieldError(key, f"{name}不能小于 {lo}"))
        if hi is not None and value > hi:
            out.append(FieldError(key, f"{name}不能大于 {hi}"))
        # Timeout range (FR-GRID-005).
        tlo, thi = rule.get("timeout_min"), rule.get("timeout_max")
        if tlo is not None and value < tlo:
            out.append(FieldError(key, f"超时值不能小于 {tlo}"))
        if thi is not None and value > thi:
            out.append(FieldError(key, f"超时值不能大于 {thi}"))

    # CAN identifier ranges (value coerced from hex -> int).
    if rule.get("can_std_id") and isinstance(value, int):
        if not (0 <= value <= CAN_STD_MAX):
            out.append(FieldError(key, f"CAN 标准帧 ID 超范围 (0..0x{CAN_STD_MAX:X})"))
    if rule.get("can_ext_id") and isinstance(value, int):
        if not (0 <= value <= CAN_EXT_MAX):
            out.append(FieldError(key, f"CAN 扩展帧 ID 超范围 (0..0x{CAN_EXT_MAX:X})"))

    return out


def validate_record(
    specs: list[FieldSpec],
    record: dict[str, Any],
    *,
    unique_checker: Optional[Callable[[str, Any], bool]] = None,
    enforce_required: bool = True,
) -> tuple[dict[str, Any], list[FieldError]]:
    """Validate a whole record. Returns ``(coerced_record, errors)``.

    Cross-field date ordering is enforced via ``date_before`` / ``date_after``
    rules that name another field key. Pass ``enforce_required=False`` to allow
    creating a draft record whose required cells are still blank.
    """
    coerced: dict[str, Any] = {}
    errors: list[FieldError] = []
    spec_by_key = {s.field_key: s for s in specs}

    for spec in specs:
        if spec.is_readonly:
            continue
        raw = record.get(spec.field_key)
        value, errs = validate_value(
            spec, raw, unique_checker=unique_checker,
            enforce_required=enforce_required)
        coerced[spec.field_key] = value
        errors.extend(errs)

    # Cross-field date relations.
    for spec in specs:
        rule = spec.rule
        for kind in ("date_before", "date_after"):
            other = rule.get(kind)
            if not other or other not in spec_by_key:
                continue
            a, b = coerced.get(spec.field_key), coerced.get(other)
            if a is None or b is None:
                continue
            if kind == "date_before" and not (a < b):
                errors.append(FieldError(
                    spec.field_key,
                    f"{spec.display_name}必须早于{spec_by_key[other].display_name}"))
            if kind == "date_after" and not (a > b):
                errors.append(FieldError(
                    spec.field_key,
                    f"{spec.display_name}必须晚于{spec_by_key[other].display_name}"))

    return coerced, errors


def has_blocking(errors: list[FieldError]) -> bool:
    return any(e.severity == "blocking" for e in errors)
