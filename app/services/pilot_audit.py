"""Collect bounded pilot facts without approving, generating or executing assets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import has_app_context
from pydantic import ValidationError

from ..extensions import db
from ..models import AiDraft, LMUser, Project, ProjectModel, RunEvidence, Task, TestItemRow
from . import run_evidence_service as evidence
from .ai.output_validation import procedure_entries
from .ai.runtime_validation import _row
from .lanmatrix import permissions, users_service
from .lanmatrix.silver_json_export import build_documents
from .pilot_contract import AuditSnapshot, DraftObservation, PilotInput, RunObservation
from .run_validation_service import ConversionError, validate_documents


class PilotAuditError(RuntimeError):
    """A safe typed failure, with no database or filesystem details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class _Candidate:
    draft_id: int
    item_id: int
    payload: dict
    execution: dict


def _positive(value):
    return type(value) is int and value > 0


def _json_object(raw):
    def reject_constant(value):
        raise ValueError("Non-finite JSON")

    def unique_object(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _provider_kind(meta):
    receipt = meta.get("provider_provenance")
    fields = {"schema_version", "attempted_calls", "successful_calls", "api_response_calls"}
    if not isinstance(receipt, dict) or set(receipt) != fields:
        return "unknown"
    if any(type(receipt[key]) is not int for key in fields):
        return "unknown"
    if (receipt["schema_version"] == 1 and receipt["successful_calls"] > 0
            and receipt["api_response_calls"] == receipt["successful_calls"]
            and receipt["attempted_calls"] >= receipt["successful_calls"]):
        return "live"
    return "unknown"


def _execution(payload, document, *, archived_libraries=None):
    inputs = payload.get("runtime_inputs")
    if (not isinstance(inputs, dict) or not isinstance(inputs.get("constants"), list)
            or not isinstance(inputs.get("libraries"), list)
            or any(not isinstance(entry, dict) for entry in inputs["constants"] + inputs["libraries"])):
        raise ConversionError("Missing runtime input snapshot")
    constants = [_row(values) for values in inputs["constants"]]
    libraries = []
    for values in inputs["libraries"]:
        values = dict(values)
        name = values.get("lib_func") or values.get("lib_name") or values.get("case_id") or ""
        if archived_libraries is not None and name in archived_libraries:
            values["lib_stb"] = archived_libraries[name]
        libraries.append(_row(values))
    return build_documents(_row({"steps": document}, case_id="PILOT-AUDIT"), constants, libraries)


def _preflight(pilot, project):
    issues = []
    if project.status != "active":
        issues.append("project_not_active")
    ids = [viewpoint.item_id for viewpoint in pilot.viewpoints]
    rows = (TestItemRow.query.filter(TestItemRow.project_id == pilot.project_id, TestItemRow.id.in_(ids),
                                    TestItemRow.sheet == "test", TestItemRow.deleted_at.is_(None))
            .populate_existing().all())
    by_id = {row.id: row for row in rows}
    for viewpoint in pilot.viewpoints:
        row = by_id.get(viewpoint.item_id)
        if row is None:
            issues.append("row_unavailable")
            continue
        if row.version != viewpoint.version:
            issues.append("row_version_mismatch")
        if row.module != viewpoint.module:
            issues.append("row_module_mismatch")
    model = (ProjectModel.query.filter_by(project_id=pilot.project_id, id=pilot.model.model_id, deprecated_at=None)
             .populate_existing().first())
    if model is None:
        issues.append("model_unavailable")
    else:
        if (model.version or "") != pilot.model.version:
            issues.append("model_version_mismatch")
        try:
            if not model.sil_path:
                raise OSError("Missing model")
            path = Path(model.sil_path)
            path = evidence.checked_path(path.parent, path, exists=True)
            if not path.is_file():
                raise OSError("Missing model")
            if evidence._digest(path) != pilot.model.sha256:
                issues.append("model_digest_mismatch")
        except (OSError, evidence.EvidenceError):
            issues.append("model_file_unavailable")
    return list(dict.fromkeys(issues)), by_id


def _source_refs(pilot, payload):
    context = payload.get("_context")
    if (not isinstance(context, dict) or type(context.get("schema_version")) is not int
            or context["schema_version"] != 1 or not isinstance(context.get("items"), list)
            or not isinstance(context.get("provenance"), list)):
        raise ValueError("draft_context_invalid")
    if type(context.get("project_id")) is not int or context["project_id"] != pilot.project_id:
        raise ValueError("draft_scope_mismatch")
    model = context.get("model")
    if (not isinstance(model, dict) or not _positive(model.get("id"))
            or model["id"] != pilot.model.model_id or model.get("version") != pilot.model.version
            or "model_id" in payload and (type(payload["model_id"]) is not int or payload["model_id"] != model["id"])):
        raise ValueError("draft_model_mismatch")
    versions = {}
    for item in context["items"]:
        if (not isinstance(item, dict) or not _positive(item.get("id"))
                or not _positive(item.get("version")) or item["id"] in versions):
            raise ValueError("draft_context_invalid")
        versions[item["id"]] = item["version"]
    viewpoints = payload.get("viewpoints")
    if viewpoints is None:
        viewpoints = [{"ref": "legacy", "item_id": payload.get("item_id"),
                       "version": versions.get(payload.get("item_id"))}]
    if not isinstance(viewpoints, list) or not viewpoints:
        raise ValueError("draft_context_invalid")
    selection = {entry.item_id: entry.version for entry in pilot.viewpoints}
    refs = {}
    seen_ids = set()
    seen_refs = set()
    issues = []
    for entry in viewpoints:
        if (not isinstance(entry, dict) or not isinstance(entry.get("ref"), str) or not entry["ref"].strip()
                or entry["ref"] in seen_refs or not _positive(entry.get("item_id")) or entry["item_id"] in seen_ids
                or not _positive(entry.get("version")) or versions.get(entry["item_id"]) != entry["version"]):
            raise ValueError("draft_context_invalid")
        seen_ids.add(entry["item_id"])
        seen_refs.add(entry["ref"])
        identity = entry["item_id"]
        if identity not in selection:
            issues.append("draft_item_outside_selection")
        elif entry["version"] != selection[identity]:
            issues.append("draft_context_versions_mismatch")
        else:
            refs[entry["ref"]] = identity
    return refs, issues


def _accepted(draft, output, refs, generated, issues):
    if draft.status != "approved":
        return set()
    if not _positive(draft.reviewed_by) or draft.reviewed_at is None:
        issues.append("draft_review_metadata_missing")
        return set()
    try:
        result = _json_object(draft.applied_result_json)
        if "procedures" not in output:
            identity = result.get("item_id")
            if not _positive(identity) or identity != refs.get("legacy") or identity not in generated:
                raise ValueError("Invalid applied identity")
            return {identity}
        entries = result.get("applied")
        if not isinstance(entries, list):
            raise ValueError("Invalid applied result")
        accepted = set()
        seen_refs = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("ref"), str)
                    or entry["ref"] in seen_refs or not _positive(entry.get("item_id"))):
                raise ValueError("Invalid applied identity")
            seen_refs.add(entry["ref"])
            identity = entry["item_id"]
            if identity != refs.get(entry["ref"]) or identity not in generated:
                issues.append("draft_applied_result_invalid")
            else:
                accepted.add(identity)
        return accepted
    except (ValueError, TypeError):
        issues.append("draft_applied_result_invalid")
        return set()


def _draft(pilot, identity):
    draft = AiDraft.query.filter_by(id=identity, project_id=pilot.project_id, scenario="procedure").populate_existing().first()
    if draft is None:
        return DraftObservation(draft_id=identity, status="error", issues=["draft_unavailable"]), []
    statuses = {"running", "pending", "approved", "rejected", "error", "cancelled"}
    observation = DraftObservation(draft_id=identity, status=draft.status if draft.status in statuses else "error")
    if draft.status not in statuses:
        observation.issues.append("draft_status_invalid")
    try:
        meta = _json_object(draft.meta_json)
    except (ValueError, TypeError):
        meta = {}
        observation.issues.append("draft_metadata_invalid")
    observation.provider_kind = _provider_kind(meta)
    if observation.provider_kind == "unknown":
        observation.issues.append("provider_provenance_unknown")
    try:
        payload = _json_object(draft.input_json)
        output = _json_object(draft.output_json)
        refs, issues = _source_refs(pilot, payload)
        observation.issues.extend(issues)
    except (ValueError, TypeError) as failure:
        code = str(failure)
        observation.issues.append(code if code in {"draft_context_invalid", "draft_scope_mismatch", "draft_model_mismatch"}
                                  else "draft_context_invalid")
        return observation, []
    entries, problems = procedure_entries(output)
    if problems:
        observation.issues.append("draft_output_invalid")
        return observation, []
    generated = set()
    converted = {}
    for entry in entries:
        ref = entry.get("ref") if "procedures" in output else "legacy"
        item_id = refs.get(ref)
        if item_id is None:
            continue
        document = entry.get("steps_doc")
        if not isinstance(document, dict) or not isinstance(document.get("steps"), list) or not document["steps"]:
            observation.issues.append("draft_output_invalid")
            continue
        generated.add(item_id)
        try:
            if entry.get("missing_variables"):
                raise ConversionError("Missing procedure variables")
            converted[item_id] = _execution(payload, document)
        except (ConversionError, ValueError, TypeError, KeyError, AttributeError):
            observation.issues.append("draft_conversion_failed")
    accepted = _accepted(draft, output, refs, generated, observation.issues)
    observation.generated_item_ids = sorted(generated)
    observation.accepted_item_ids = sorted(accepted)
    observation.conversion_item_ids = sorted(converted)
    observation.issues = list(dict.fromkeys(observation.issues))
    candidates = []
    if observation.provider_kind == "live":
        candidates = [_Candidate(identity, item_id, payload, converted[item_id])
                      for item_id in sorted(accepted & converted.keys())]
    return observation, candidates


def _linked_draft(data, item_id, candidates):
    try:
        approved = _json_object(data["approved_inputs"])
        libraries = _json_object(approved.get("libraries"))
        constants = _json_object(approved.get("constants"))
        document = _json_object(approved.get("test"))
        documents = data["documents"]
        cases = [value for name, value in documents.items() if name.startswith("testcase_") and name.endswith(".json")]
        if len(cases) != 1:
            return None
        archived = {"testcase": {**_json_object(cases[0]), "test_case_id": "PILOT-AUDIT"},
                    "constants": _json_object(documents.get("constants.json")),
                    "library": _json_object(documents.get("lib.json"))}
        validate_documents(archived["testcase"], archived["constants"], archived["library"])
        if _canonical(constants) != _canonical(archived["constants"]):
            return None
        matches = []
        for candidate in candidates:
            if candidate.item_id != item_id:
                continue
            if set(libraries) != set(candidate.execution["library"]["subroutines"]):
                continue
            pinned = _execution(candidate.payload, document, archived_libraries=libraries)
            if _canonical(pinned) == _canonical(candidate.execution) == _canonical(archived):
                matches.append(candidate.draft_id)
        return matches[0] if len(matches) == 1 else None
    except (ConversionError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _run(pilot, attempt, rows, candidates):
    observation = RunObservation(task_key=attempt.task_key, run_count=attempt.run_count, item_id=attempt.item_id)
    task = Task.query.filter_by(project_id=pilot.project_id, task_key=attempt.task_key, deleted_at=None).populate_existing().first()
    if task is None:
        observation.issues.append("run_unavailable")
        return observation
    record = db.session.get(RunEvidence, (task.id, attempt.run_count), populate_existing=True)
    if record is None or not record.manifest_sha256:
        observation.issues.append("run_evidence_missing")
        return observation
    observation.finalised = bool(record.finalised)
    try:
        root = evidence.attempt_dir(task, attempt.run_count)
        if not (root / "manifest.json").is_file():
            observation.issues.append("run_evidence_missing")
            return observation
        data = evidence.read_evidence(task, attempt.run_count, max_log_bytes=0)
    except (evidence.EvidenceError, OSError, ValueError, TypeError, KeyError, AttributeError):
        observation.issues.append("run_evidence_invalid")
        return observation
    outcome = data.get("outcome")
    if not isinstance(outcome, dict):
        observation.issues.append("run_not_sealed")
        return observation
    if not observation.finalised:
        observation.issues.append("run_not_finalised")
    approved = data.get("approved_inputs")
    saved = approved.get("row") if isinstance(approved, dict) else None
    row = rows.get(attempt.item_id)
    if (not isinstance(saved, dict) or row is None or type(saved.get("id")) is not int
            or saved["id"] != attempt.item_id or saved.get("uuid") != row.uuid or not _positive(saved.get("version"))):
        observation.issues.append("run_row_identity_mismatch")
    model = data.get("model")
    if (not isinstance(model, dict) or type(model.get("id")) is not int or model["id"] != pilot.model.model_id
            or model.get("version") != pilot.model.version or model.get("sha256") != pilot.model.sha256
            or not isinstance(model.get("files"), dict) or model["files"].get("approved.sil") != pilot.model.sha256):
        observation.issues.append("run_model_mismatch")
    artifacts = {Path(entry["path"]).name.lower() for entry in data.get("artifacts", []) if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    if not {"console.log", "jdgrslt.log", "output.csv"}.issubset(artifacts):
        observation.issues.append("run_artifacts_missing")
    status = outcome.get("status")
    verdict = outcome.get("verdict")
    if (not isinstance(status, str) or status not in {"passed", "failed", "cancelled"}
            or not isinstance(verdict, str) or verdict not in {"PASS", "FAIL", "ERROR", "CANCELLED", "UNTESTABLE", "Untestable"}):
        observation.issues.append("run_outcome_invalid")
    else:
        observation.status = status
        observation.verdict = verdict
    backend = outcome.get("runner_backend")
    observation.runner_backend = backend if isinstance(backend, str) and backend in {"silver", "mock"} else "unknown"
    kind = outcome.get("evidence_kind")
    if backend == "mock" or kind == "synthetic":
        observation.evidence_kind = "synthetic"
    elif kind == "silver_runtime" and backend == "silver":
        observation.evidence_kind = "silver_runtime"
    observation.verified = not observation.issues
    if observation.evidence_kind == "synthetic":
        observation.issues.append("run_synthetic")
    elif observation.evidence_kind == "unclassified":
        observation.issues.append("run_unclassified")
    if observation.verified:
        observation.approved_draft_id = _linked_draft(data, attempt.item_id, candidates)
        if observation.approved_draft_id is None:
            observation.issues.append("run_draft_link_missing")
    return observation


def collect_snapshot(pilot: PilotInput, *, actor_id: int) -> AuditSnapshot:
    """Own a fresh read-only transaction in an existing, clean Flask session."""
    if not has_app_context():
        raise PilotAuditError("APP_CONTEXT_REQUIRED")
    if not isinstance(pilot, PilotInput):
        raise PilotAuditError("PILOT_INPUT_INVALID")
    try:
        pilot = PilotInput.model_validate(pilot.model_dump())
    except ValidationError:
        raise PilotAuditError("PILOT_INPUT_INVALID") from None
    session = db.session()
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise PilotAuditError("SESSION_NOT_CLEAN")
    if db.engine.dialect.name != "postgresql":
        raise PilotAuditError("POSTGRESQL_REQUIRED")
    try:
        session.expire_all()
        session.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
        with session.no_autoflush:
            actor = LMUser.query.filter_by(id=actor_id).populate_existing().first() if _positive(actor_id) else None
            project = Project.query.filter_by(id=pilot.project_id, deleted_at=None).populate_existing().first()
            if actor is None or not actor.is_active or project is None:
                raise PilotAuditError("PROJECT_ACCESS_DENIED")
            try:
                permissions.require("project.view", users_service.role_in_project(project.id, actor),
                                    is_system_admin=actor.is_system_admin)
            except permissions.PermissionDenied:
                raise PilotAuditError("PROJECT_ACCESS_DENIED") from None
            issues, rows = _preflight(pilot, project)
            drafts = []
            candidates = []
            for identity in pilot.draft_ids:
                observation, generated = _draft(pilot, identity)
                drafts.append(observation)
                candidates.extend(generated)
            runs = [_run(pilot, attempt, rows, candidates) for attempt in pilot.run_attempts]
            return AuditSnapshot(selection_sha256=pilot.selection_digest(), source="postgresql_readonly",
                                 preflight_issues=issues, drafts=drafts, runs=runs)
    except PilotAuditError:
        raise
    except Exception:
        raise PilotAuditError("AUDIT_FAILED") from None
    finally:
        session.rollback()
