"""Validate model transport fields before the append-only row verifier."""

from __future__ import annotations

import json
import re
from typing import Any


def parse(reply: str) -> Any:
    def unique_fields(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ValueError(f"Duplicate JSON key: {key}")
            record[key] = value
        return record

    return json.loads(reply, object_pairs_hook=unique_fields)


def schema_errors(parsed: Any) -> list[dict[str, str]]:
    """Reject malformed fields atomically; domain/witness checks stay external.

    rows and declined may be omitted when the other is present; rationale and
    row reasons are optional, matching existing callers. Declines require an
    explicit reason. Supplied optional fields must always have their declared
    types, including when empty or null.
    """
    errors = []

    def error(path, message):
        errors.append({"path": path, "message": message})

    def fields(value, path, allowed):
        for key in sorted(value.keys() - allowed):
            error(f"{path}.{key}", "unknown field")

    def text(value, path, *, nonempty=False):
        if not isinstance(value, str) or (nonempty and not value.strip()):
            error(path, "must be a non-empty string" if nonempty else "must be a string")

    if not isinstance(parsed, dict):
        return [{"path": "$", "message": "must be a JSON object"}]
    fields(parsed, "$", {"rows", "declined", "rationale"})
    if "rows" not in parsed and "declined" not in parsed:
        error("$", "must supply rows or declined")
    if "rationale" in parsed:
        text(parsed["rationale"], "$.rationale")
    for key in ("rows", "declined"):
        records = parsed.get(key, [])
        if not isinstance(records, list):
            error(f"$.{key}", "must be an array")
            continue
        for index, record in enumerate(records):
            path = f"$.{key}[{index}]"
            if not isinstance(record, dict):
                error(path, "must be an object")
                continue
            fields(record, path, {"obligation", "base_row", "inputs", "reason"} if key == "rows"
                   else {"obligation", "reason"})
            text(record.get("obligation"), f"{path}.obligation", nonempty=True)
            if key == "declined":
                text(record.get("reason"), f"{path}.reason", nonempty=True)
                continue
            if "reason" in record:
                text(record["reason"], f"{path}.reason")
            if type(record.get("base_row")) is not int:
                error(f"{path}.base_row", "must be an integer row number")
            inputs = record.get("inputs")
            if not isinstance(inputs, dict) or not inputs:
                error(f"{path}.inputs", "must be a non-empty object of string values")
                continue
            for token, value in inputs.items():
                if not re.fullmatch(r"I[1-9][0-9]*", token):
                    error(f"{path}.inputs.{token}", "must use an input token such as I1")
                if not isinstance(value, str) or not re.fullmatch(r"[+-]?(?:[0-9]+|0[xX][0-9a-fA-F]+)", value):
                    error(f"{path}.inputs.{token}", "must be a decimal or hexadecimal integer string")
    return errors
