"""Source-free, attempt-bound offline input-row diagnostics."""

from __future__ import annotations

import csv
import hashlib
import io
from typing import Any

from . import increment


def report_records(check: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Read record objects safely; gap_report still rejects any invalid entry."""
    completeness = check.get("completeness")
    records = completeness.get(key) if isinstance(completeness, dict) else None
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def _worklist_errors(report: dict[str, Any], key: str) -> list[str]:
    records, count = report.get(key), report.get(f"{key}_count")
    errors = []
    if not isinstance(records, list):
        errors.append(f"{key} must be a supplied array")
    if type(count) is not int or count < 0:
        errors.append(f"{key}_count must be a supplied non-negative integer")
    if isinstance(records, list):
        ids = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                errors.append(f"{key}[{index}] must be an object")
                continue
            oid = record.get("obligation_id")
            if not isinstance(oid, str) or not oid.strip():
                errors.append(f"{key}[{index}].obligation_id must be a non-empty string")
            elif oid in ids:
                errors.append(f"{key} contains duplicate obligation_id {oid}")
            else:
                ids.add(oid)
            if "requirement" in record and not isinstance(record["requirement"], dict):
                errors.append(f"{key}[{index}].requirement must be an object")
        if type(count) is int and count >= 0 and count != len(records):
            errors.append(f"{key}_count disagrees with the supplied array length")
    return errors


def gap_report(check: dict[str, Any]) -> dict[str, Any]:
    """Validate current worklists and the known legacy unresolved-only format."""
    completeness = check.get("completeness")
    if not isinstance(completeness, dict) or not completeness:
        return {"details_complete": False, "supplied_unresolved": 0,
                "reported_unresolved": None, "supplied_output_pending": 0,
                "reported_output_pending": None, "output_pending_complete": False,
                "output_pending_reported": False, "errors": ["completeness must be a non-empty object"]}
    unresolved = completeness.get("unresolved")
    pending = completeness.get("output_pending")
    flag = completeness.get("details_complete", True)
    flag_errors = [] if type(flag) is bool else ["details_complete must be a Boolean when supplied"]
    unresolved_errors = _worklist_errors(completeness, "unresolved")
    # Older checker reports supply only the unresolved list/count pair. If
    # either pending-output field is supplied, the entire pair is required.
    pending_reported = "output_pending" in completeness or "output_pending_count" in completeness
    pending_errors = _worklist_errors(completeness, "output_pending") if pending_reported else []
    declared_complete = flag is True and not flag_errors
    unresolved_complete = declared_complete and not unresolved_errors
    pending_complete = declared_complete and not pending_errors
    errors = flag_errors + unresolved_errors + pending_errors
    if flag is False:
        errors.append("checker declared details_complete=false")
    return {"details_complete": unresolved_complete and pending_complete,
            "supplied_unresolved": len(unresolved) if isinstance(unresolved, list) else 0,
            "reported_unresolved": completeness.get("unresolved_count"),
            "supplied_output_pending": len(pending) if isinstance(pending, list) else 0,
            "reported_output_pending": completeness.get("output_pending_count"),
            "output_pending_complete": pending_complete, "output_pending_reported": pending_reported,
            "errors": errors}


def configuration_conflicts(check: dict[str, Any]) -> list[str]:
    """Keep configuration conflicts separate from work an input row can close."""
    completeness = check.get("completeness")
    completeness = completeness if isinstance(completeness, dict) else {}
    conflicts = []
    for context in (check.get("test_requirements"), completeness.get("test_requirements")):
        if isinstance(context, dict) and context.get("status") == "NEEDS_REVIEW":
            conflicts.append("test_requirements")
    for item in [*report_records(check, "unresolved"), *report_records(check, "output_pending")]:
        requirement = item.get("requirement")
        if isinstance(requirement, dict) and requirement.get("configuration_status") == "NEEDS_REVIEW":
            conflicts.append(str(item.get("obligation_id") or "unidentified obligation"))
    return sorted(set(conflicts))


def integrity_errors(check: dict[str, Any], expected_sha: str = "",
                     execution: dict[str, Any] | None = None) -> list[str]:
    """Compare supplied identities; legacy reports may omit optional digests."""
    witnesses = check.get("candidate_witnesses") or {}
    identities = {"check.candidate_sha256": check.get("candidate_sha256"),
                  "witness.candidate_sha256": witnesses.get("candidate_sha256")}
    for label, record in (("check", check), ("execution", execution or {})):
        identities[f"{label}.input_binding.candidate.sha256"] = (
            ((record.get("input_binding") or {}).get("candidate") or {}).get("sha256"))
    identities["execution.candidate_sha256"] = (execution or {}).get("candidate_sha256")
    anchor = expected_sha or next((value for value in identities.values() if value), "")
    errors = [f"{name} disagrees with candidate SHA" for name, value in identities.items()
              if value and anchor and value != anchor]
    for label, record in (("check", check), ("execution", execution or {})):
        if (record.get("evidence_integrity") or {}).get("status") == "UNKNOWN":
            errors.append(f"{label} has unresolved integrity evidence")
        if any(issue.get("code") == "INTEGRITY_ERROR" for issue in record.get("issues") or []):
            errors.append(f"{label} reports an integrity error")
    return errors


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
    integrity = integrity_errors(check, digest, attempt.get("execution"))
    identified = (not integrity and witnesses.get("contract") == "ut-agent.candidate-witness/1"
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
            "mode": "APPEND_INPUT_ROWS_ONLY", "allowed_obligations": sorted(allowed)
            if report["details_complete"] and not configuration_conflicts(check) and not integrity else [],
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
