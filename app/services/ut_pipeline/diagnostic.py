"""Source-free, attempt-bound offline input-row diagnostics."""

from __future__ import annotations

import csv
import hashlib
import io
from typing import Any

from . import increment


def gap_report(check: dict[str, Any]) -> dict[str, Any]:
    """Check both machine worklists, including reports from older checkers."""
    completeness = check.get("completeness") or {}
    unresolved = len(completeness.get("unresolved") or [])
    pending = len(completeness.get("output_pending") or [])
    declared_complete = completeness.get("details_complete") is not False
    unresolved_complete = declared_complete and completeness.get("unresolved_count", unresolved) == unresolved
    pending_complete = declared_complete and completeness.get("output_pending_count", pending) == pending
    return {"details_complete": unresolved_complete and pending_complete,
            "supplied_unresolved": unresolved, "reported_unresolved": completeness.get("unresolved_count"),
            "supplied_output_pending": pending, "reported_output_pending": completeness.get("output_pending_count"),
            "output_pending_complete": pending_complete}


def build_packet(*, project_id: int, job_id: int, attempt: dict[str, Any],
                 target: dict[str, Any], csv_bytes: bytes, constraints: dict[str, Any],
                 provenance: dict[str, Any], allowed: set[str],
                 editable_columns: set[int] | None) -> dict[str, Any]:
    """Export complete input vectors, never CSV comments or source artifacts.

    The caller supplies the same obligation and column scope as the existing
    append-only repair verifier. This function neither solves nor applies rows.
    """
    digest = hashlib.sha256(csv_bytes).hexdigest()
    if not attempt.get("csv_sha256") or digest != attempt["csv_sha256"]:
        raise ValueError("Candidate CSV hash does not match the selected attempt")
    text = csv_bytes.decode("cp932")
    table = increment.parse(text)
    try:
        source_rows = [row for row in csv.reader(io.StringIO(text, newline=""), strict=True)
                       if row and row[0] == ""]
    except csv.Error as exc:
        raise ValueError("Candidate CSV has malformed records; repair its format before exporting") from exc
    if source_rows != table.rows:
        raise ValueError("Candidate CSV has malformed data rows; cannot export a complete input set")
    check = attempt.get("check") or {}
    if check.get("candidate_sha256") and check["candidate_sha256"] != digest:
        raise ValueError("Check belongs to a different candidate CSV")
    domains = (check.get("input_ranges") or {}).get("contract") or {}
    columns = domains.get("columns") or []
    exact = (domains.get("contract") == "ut-agent.input-domains/1"
             and len(columns) == len(table.inputs)
             and all(c.get("index") == i and c.get("column") == name
                     for i, (c, name) in enumerate(zip(columns, table.inputs), 1)))
    witnesses = check.get("candidate_witnesses") or {}
    identified = (witnesses.get("contract") == "ut-agent.candidate-witness/1"
                  and witnesses.get("candidate_sha256") == digest
                  and check.get("candidate_sha256") == digest
                  and witnesses.get("input_mapping_identified") is True
                  and (witnesses.get("evidence") or {}).get("identified") is True
                  and exact)
    report = gap_report(check)
    tokens = [table.token(i) for i in range(len(table.inputs))]
    editable = tokens if editable_columns is None else [table.token(i) for i in sorted(editable_columns)]
    return {
        "contract": "ut-platform.diagnostic-packet/2",
        "project_id": project_id, "job_id": job_id, "attempt_id": attempt["id"],
        "version": {key: attempt.get(key) for key in ("seq", "origin", "created_at")},
        "candidate_sha256": digest, "candidate_encoding": "cp932",
        "target": target, "check": check, "execution": attempt.get("execution"),
        "test_configuration": constraints, "provenance": provenance,
        "inputs": {
            "columns": [{"token": table.token(i), "index": i + 1, "column": name,
                         "domain": columns[i] if exact else None}
                        for i, name in enumerate(table.inputs)],
            "domain_mapping_status": "EXACT" if exact else "UNKNOWN",
            "reported_domain_contract": domains,
            "rows": [{"base_row": n, "inputs": dict(zip(tokens, row[1:1 + len(tokens)]))}
                     for n, row in enumerate(table.rows, 1)],
            "row_count": len(table.rows), "rows_complete": True,
        },
        "typed_evidence_status": "IDENTIFIED" if identified else "UNKNOWN",
        "gap_report": report,
        "modification_boundary": {
            "mode": "APPEND_INPUT_ROWS_ONLY", "allowed_obligations": sorted(allowed) if report["details_complete"] else [],
            "editable_input_tokens": editable, "max_rows": increment.MAX_ROWS_PER_ROUND,
            "base_row": "1-based data row; 0 means all inputs blank; omitted inputs copy base_row",
            "existing_rows": "immutable", "columns": "immutable", "test_policy": "immutable",
            "expected_values": "new rows stay blank; execution capture only",
            "unidentified_mapping": "decline; never infer binding from source, labels or similar names",
        },
        "reply_contract": {
            "format": "JSON object only; never CSV",
            "example": {"rows": [{"obligation": "<allowed obligation ID>", "base_row": 0,
                                  "inputs": {"I1": "<value>"}, "reason": "<typed evidence>"}],
                        "declined": [{"obligation": "<ID>", "reason": "<missing evidence>"}],
                        "rationale": "<summary>"},
            "validation": ["Check selected attempt ID and SHA again before using any reply",
                           "app.services.ut_pipeline.increment.apply with allowed obligations, domains and editable columns",
                           "Existing ut-agent candidate-witness verification before execution",
                           "Execution, recheck and human review; claims are not proof"],
            "auto_apply": False,
        },
        "instruction": "Use the complete check and typed path evidence to explain each gap. "
                       "Propose only appended input rows within modification_boundary. "
                       "Unknown bindings, incomplete reports or conflicting configuration require declined. "
                       "Do not infer source semantics, invent row locations for missing cases, guess expected values, "
                       "weaken policy or treat HOST_EXECUTION as target validation. 证据不足须明确说明。",
    }
