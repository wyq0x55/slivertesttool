"""Apply one human decision atomically through the existing asset services."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any

from flask import current_app

from ...extensions import db
from ...models import CellComment, Project, ProjectModel, SbsRevision, TestItemRow
from ...models.ai_draft import AiDraft
from ..lanmatrix import items_service, permissions, service, sbs_service
from ..lanmatrix.service import ServiceError
from . import validators
from .output_validation import item_snapshots, procedure_entries

_KIND_JP = {"normal": "正例", "abnormal": "反例", "boundary": "境界値", "combination": "組合せ"}


class ApplyError(RuntimeError):
    pass


def lock_draft(draft_id: int) -> AiDraft | None:
    with db.session.no_autoflush:
        return (AiDraft.query.filter_by(id=draft_id).populate_existing()
                .with_for_update().first())


def _json_object(raw, name):
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError) as exc:
        raise ApplyError(f"Invalid {name}; regenerate this draft") from exc
    if not isinstance(value, dict):
        raise ApplyError(f"Invalid {name}; regenerate this draft")
    return value


def _guard_row_writes(project_id):
    if not current_app.config.get("COLLAB_REST_GUARD", True):
        return
    from ...collab import presence
    try:
        active = presence.is_collab_active(project_id)
    except Exception as exc:
        raise ApplyError("Collaboration state unavailable; approval is blocked") from exc
    if active:
        raise ApplyError("Active collaboration owns row mutations; approval is blocked")


def _lock_items(project, snapshots):
    if not snapshots:
        return {}
    rows = (TestItemRow.query.filter(TestItemRow.id.in_(sorted(snapshots)))
            .order_by(TestItemRow.id).populate_existing().with_for_update().all())
    by_id = {row.id: row for row in rows}
    for identity, version in snapshots.items():
        row = by_id.get(identity)
        if (row is None or row.project_id != project.id or row.deleted_at is not None
                or row.sheet != "test" or row.version != version):
            raise ApplyError(f"Generation item {identity} is stale, deleted or unavailable; regenerate this draft")
    return by_id


def _lock_model(project, payload, scenario):
    snapshot = payload["_context"].get("model")
    if snapshot is None:
        if scenario == "sbs" or payload.get("model_id") is not None:
            raise ApplyError("Missing saved-model generation snapshot; regenerate this draft")
        return None
    if (not isinstance(snapshot, dict) or type(snapshot.get("id")) is not int
            or snapshot["id"] < 1 or not isinstance(snapshot.get("name"), str)
            or not isinstance(snapshot.get("version"), str)
            or payload.get("model_id") != snapshot["id"]):
        raise ApplyError("Invalid saved-model generation identity; regenerate this draft")
    model = (ProjectModel.query.filter_by(id=snapshot["id"]).populate_existing()
             .with_for_update().first())
    if (model is None or model.project_id != project.id or model.deprecated_at is not None
            or model.name != snapshot["name"] or (model.version or "") != snapshot["version"]):
        raise ApplyError("Saved model changed or is unavailable; regenerate this draft")
    if model.kind == "bundle":
        expected_sha = snapshot.get("sbs_sha256")
        saved = sbs_service.read_sbs(project.id, model.name, model_id=model.id)
        if not expected_sha or saved["version"] != expected_sha:
            raise ApplyError("Saved SBS changed or has no generation hash; regenerate this draft")
        if scenario == "sbs":
            base = payload.get("current_sbs")
            if not isinstance(base, str) or sbs_service._sha(base) != expected_sha:
                raise ApplyError("SBS base does not match the generation snapshot; regenerate this draft")
    elif scenario == "sbs":
        raise ApplyError("SBS approval requires a saved bundle model")
    return model


def _require_fields(project, sheet, keys):
    writable = {spec.field_key for spec in items_service.field_specs(project.id)
                if spec.sheet == sheet and not spec.is_readonly}
    missing = set(keys) - writable
    if missing:
        raise ApplyError("Required writable asset fields are unavailable: " + ", ".join(sorted(missing)))


def _provenance(draft, payload):
    sources = payload["_context"]["provenance"]
    encoded = json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"draft_id": draft.id, "provenance": {
        "sources": sources, "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()},
        "model_snapshot": payload["_context"].get("model")}


def apply_draft(draft: AiDraft, reviewer, refs: list[str] | None = None) -> dict[str, Any]:
    try:
        draft = lock_draft(draft.id)
        if draft is None or draft.status not in (AiDraft.STATUS_PENDING, AiDraft.STATUS_ERROR):
            raise ApplyError("Draft is missing or already processed")
        project = (Project.query.filter_by(id=draft.project_id).populate_existing()
                   .with_for_update().first())
        if project is None or project.deleted_at is not None:
            raise ApplyError("Project is unavailable")
        permissions.require("item.edit", service.role_in_project(project.id, reviewer),
                            is_system_admin=reviewer.is_system_admin)
        payload = _json_object(draft.input_json, "generation input")
        output = _json_object(draft.output_json, "output")
        problems = validators.validate_output(draft.scenario, payload, output, refs=refs, for_apply=True)
        if problems:
            raise ApplyError("; ".join(problems))
        if payload["_context"]["project_id"] != project.id:
            raise ApplyError("Generation context belongs to another project")
        if draft.scenario in ("viewpoint", "procedure", "lib"):
            _guard_row_writes(project.id)
        snapshots, _problems = item_snapshots(draft.scenario, payload, output, refs)
        rows = _lock_items(project, snapshots)
        model = _lock_model(project, payload, draft.scenario)
        if draft.scenario == "lib":
            existing_rows = (TestItemRow.query.filter_by(project_id=project.id, sheet="lib", deleted_at=None)
                             .populate_existing().with_for_update().all())
            names = {str(row.get_field("lib_func") or row.get_field("lib_name") or row.case_id or "").strip()
                     for row in existing_rows}
            if output["lib_name"] in names:
                raise ApplyError("Library name already exists")
        evidence = _provenance(draft, payload)
        applier = {"viewpoint": _apply_viewpoint, "procedure": _apply_procedure,
                   "sbs": _apply_sbs, "lib": _apply_lib, "failure": _apply_failure}[draft.scenario]
        result = applier(draft, reviewer, project, payload, output, rows, snapshots, model, evidence, refs)
        result.update(evidence)
        draft.status = AiDraft.STATUS_APPROVED
        draft.reviewed_by = reviewer.id
        draft.reviewed_at = datetime.datetime.utcnow()
        draft.applied_result_json = json.dumps(result, ensure_ascii=False, indent=2)
        db.session.commit()
        return result
    except ServiceError as exc:
        db.session.rollback()
        raise ApplyError(f"Asset service rejected approval: {exc}") from exc
    except Exception:
        db.session.rollback()
        raise


def _apply_viewpoint(draft, reviewer, project, payload, output, rows, snapshots, model, evidence, refs):
    module_id = output["module_id"]
    created_ids = []
    for viewpoint in output["viewpoints"]:
        purpose_parts = []
        if viewpoint.get("precondition"):
            purpose_parts.append(f"前提：{viewpoint['precondition']}")
        if viewpoint.get("condition"):
            purpose_parts.append(f"条件：{viewpoint['condition']}")
        values = {"test_id": viewpoint["case_id"], "test_name": viewpoint["title"],
                  "viewpoint": _KIND_JP[viewpoint["kind"]], "purpose": "；".join(purpose_parts),
                  "description": f"期待：{viewpoint['expected']}",
                  "remark": "[AI 生成观点草稿，待审核]\n" + json.dumps(evidence, ensure_ascii=False),
                  "traceability_id": module_id}
        _require_fields(project, "test", values)
        item = items_service.create_item(reviewer, project, values, draft=True, sheet="test", commit=False)
        created_ids.append(item.id)
    return {"created_item_ids": created_ids, "module_id": module_id}


def _apply_procedure(draft, reviewer, project, payload, output, rows, snapshots, model, evidence, refs):
    _require_fields(project, "test", {"steps"})
    if "procedures" not in output:
        item = rows[payload["item_id"]]
        items_service.update_item(reviewer, project, item, snapshots[item.id],
                                  {"steps": json.dumps(output["steps_doc"], ensure_ascii=False)}, commit=False)
        return {"item_id": item.id, "missing_variables": [], "note": ""}
    entries, _problems = procedure_entries(output, refs)
    by_ref = {entry["ref"]: entry for entry in payload["viewpoints"] if isinstance(entry, dict)}
    applied = []
    for entry in entries:
        item = rows[by_ref[entry["ref"]]["item_id"]]
        items_service.update_item(reviewer, project, item, snapshots[item.id],
                                  {"steps": json.dumps(entry["steps_doc"], ensure_ascii=False)}, commit=False)
        applied.append({"ref": entry["ref"], "item_id": item.id})
    selected_refs = {entry["ref"] for entry in entries}
    skipped = [{"ref": entry.get("ref"), "reason": "未勾选（部分通过）"}
               for entry in output["procedures"] if isinstance(entry, dict) and entry.get("ref") not in selected_refs]
    return {"applied": applied, "skipped": skipped, "failed_refs": output.get("failed_refs") or []}


def _apply_sbs(draft, reviewer, project, payload, output, rows, snapshots, model, evidence, refs):
    base_content = payload["current_sbs"].rstrip()
    content = (base_content + "\n\n" if base_content else "") + output["sbs_additions"]
    encoded = content.encode("utf-8", "surrogatepass")
    if len(encoded) > sbs_service.MAX_SBS_BYTES:
        raise ApplyError("SBS candidate exceeds the size limit")
    revision = SbsRevision(project_id=project.id, model_id=model.id, filename=f"ai-draft-{draft.id}.sbs",
                           content=content, sha256=hashlib.sha256(encoded).hexdigest(), size=len(encoded), author_id=reviewer.id)
    db.session.add(revision)
    db.session.flush()
    old = (SbsRevision.query.filter_by(model_id=model.id)
           .order_by(SbsRevision.created_at.desc(), SbsRevision.id.desc()).offset(sbs_service.MAX_REVISIONS).all())
    for row in old:
        db.session.delete(row)
    return {"sbs_revision_id": revision.id, "needed_variables": output.get("needed_variables") or [],
            "sbs_base_sha256": payload["_context"]["model"]["sbs_sha256"]}


def _apply_lib(draft, reviewer, project, payload, output, rows, snapshots, model, evidence, refs):
    parameters = []
    for parameter in output.get("lib_para") or []:
        default = parameter.get("default")
        parameters.append(parameter["name"] if default is None else f"{parameter['name']}={default}")
    values = {"lib_func": output["lib_name"], "lib_name": output["lib_name"],
              "lib_value": output.get("description") or "", "lib_para": "\n".join(parameters),
              "lib_stb": json.dumps(output["lib_stb"], ensure_ascii=False),
              "lib_note": "[AI 生成，人工提议触发]\n" + json.dumps(evidence, ensure_ascii=False)}
    _require_fields(project, "lib", values)
    if output.get("rewritten"):
        _require_fields(project, "test", {"steps"})
    lib_row = items_service.create_item(reviewer, project, values, draft=True, sheet="lib", commit=False)
    rewritten_ids = []
    for entry in output.get("rewritten") or []:
        item = rows[entry["item_id"]]
        items_service.update_item(reviewer, project, item, snapshots[item.id],
                                  {"steps": json.dumps(entry["steps_doc"], ensure_ascii=False)}, commit=False)
        rewritten_ids.append(item.id)
    return {"lib_item_id": lib_row.id, "rewritten_item_ids": rewritten_ids}


def _apply_failure(draft, reviewer, project, payload, output, rows, snapshots, model, evidence, refs):
    item = rows[payload["item_id"]]
    content = (f"[AI 差异分析 / {output['classification']}]\n{output['analysis']}\n\n"
               f"最可能原因：{output['likely_cause']}\n建议处理：{output['suggested_action']}\n"
               + json.dumps(evidence, ensure_ascii=False))
    comment = CellComment(project_id=project.id, test_item_id=item.id, field_key="ai_failure_analysis",
                           content=content, created_by=reviewer.id)
    db.session.add(comment)
    db.session.flush()
    return {"comment_id": comment.id, "item_id": item.id}
