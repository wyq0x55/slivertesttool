"""Lossless sharing of supplied typed repair evidence; no semantic inference."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

CONTRACT = "ut-platform.shared-repair-context/1"
# Sized for the local model's window; the whole prompt stays under repair._MAX_PROMPT_CHARS.
MAX_CONTEXT_CHARS = 32000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class PromptContext:
    text: str
    required_chars: int
    obligation_ids: list[str]

    @property
    def complete(self) -> bool:
        return self.required_chars <= MAX_CONTEXT_CHARS

    def budget(self) -> dict[str, Any]:
        return {"limit": MAX_CONTEXT_CHARS, "required_chars": self.required_chars,
                "complete": self.complete, "obligation_ids": self.obligation_ids}


def pack(records: list[dict[str, Any]]) -> PromptContext:
    """Share equal decisions and paths without dropping even unknown fields.

    Only required_outcome belongs to the path edge. Everything else in the
    supplied branch, including the typed tree, bindings and provenance, stays
    together in the decision. References are zero-based array indexes.
    """
    ordered = sorted(records, key=lambda record: str(record.get("obligation_id", "")))
    decisions: list[dict[str, Any]] = []
    paths: list[list[dict[str, Any]]] = []
    decision_indexes: dict[str, int] = {}
    path_indexes: dict[str, int] = {}
    shared = []

    def intern(value, values, indexes):
        key = _json(value)
        if key not in indexes:
            indexes[key] = len(values)
            values.append(value)
        return indexes[key]

    for record in ordered:
        if not isinstance(record.get("path"), list):
            shared.append({"record": record})
            continue
        edges = []
        for branch in record["path"]:
            if not isinstance(branch, dict):
                edges.append({"literal": branch})
                continue
            decision = {key: value for key, value in branch.items() if key != "required_outcome"}
            edge = {"decision": intern(decision, decisions, decision_indexes)}
            if "required_outcome" in branch:
                edge["required_outcome"] = branch["required_outcome"]
            edges.append(edge)
        shared.append({"record": {key: value for key, value in record.items() if key != "path"},
                       "path": intern(edges, paths, path_indexes)})
    raw = _json(ordered)
    factored = _json({"contract": CONTRACT, "decisions": decisions, "paths": paths, "records": shared})
    text = min((raw, factored), key=len)
    required_chars = len(text)
    ids = [record["obligation_id"] for record in ordered if isinstance(record.get("obligation_id"), str)]
    if required_chars > MAX_CONTEXT_CHARS:
        # No partial evidence is rendered as if it were a complete work item.
        text = _json({"status": "PROMPT_BUDGET_EXCEEDED", "required_chars": required_chars,
                      "limit": MAX_CONTEXT_CHARS, "obligation_ids": ids})
    return PromptContext(text, required_chars, ids)
