"""Apply an approved draft through the *existing* service layer.

This is the single write path: AI never mutates rows directly, it goes
through ``items_service`` / ``SbsRevision`` / ``CellComment`` exactly like
the web editor does, so field validation, row ordering and audit all keep
working unchanged.

Apply semantics per scenario:

    viewpoint  → create one Draft test row per viewpoint (sheet=test), mapped
                 onto the provisioned Test-Matrix fields (test_id / test_name /
                 viewpoint / purpose / description / traceability_id)
    procedure  → write ``steps`` onto the target ``TestItemRow``
    sbs        → append an ``SbsRevision`` snapshot for the target model
                 (activation/restoration stays with the existing revision UI)
    lib        → create a lib row (sheet=lib, callable name in ``lib_func``)
                 + rewrite referenced procedures
    failure    → attach the analysis as a ``CellComment`` on the target item
"""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any

from ...extensions import db
from ...models import CellComment, ProjectModel, SbsRevision, TestItemRow
from ...models.ai_draft import AiDraft
from ..lanmatrix import items_service
from ..lanmatrix.service import ServiceError

# viewpoint kind → テスト観点 display value
_KIND_JP = {"normal": "正例", "abnormal": "反例",
            "boundary": "境界値", "combination": "組合せ"}


class ApplyError(RuntimeError):
    pass


def apply_draft(draft: AiDraft, reviewer) -> dict[str, Any]:
    from ...models import Project
    project = db.session.get(Project, draft.project_id)
    if project is None:
        raise ApplyError("项目不存在")
    if draft.status not in (AiDraft.STATUS_PENDING, AiDraft.STATUS_ERROR):
        raise ApplyError(f"草稿已处理（{draft.status}）")
    output = json.loads(draft.output_json) if draft.output_json else {}
    applier = {
        "viewpoint": _apply_viewpoint,
        "procedure": _apply_procedure,
        "sbs": _apply_sbs,
        "lib": _apply_lib,
        "failure": _apply_failure,
    }.get(draft.scenario)
    if applier is None:
        raise ApplyError(f"未知场景：{draft.scenario}")
    try:
        result = applier(draft, reviewer, project, output)
    except ServiceError as exc:
        db.session.rollback()
        raise ApplyError(f"落库被字段校验拒绝：{exc}") from exc
    draft.status = AiDraft.STATUS_APPROVED
    draft.reviewed_by = reviewer.id
    draft.reviewed_at = datetime.datetime.utcnow()
    draft.applied_result_json = json.dumps(
        result, ensure_ascii=False, indent=2)
    db.session.commit()
    return result


def _input_payload(draft: AiDraft) -> dict[str, Any]:
    return json.loads(draft.input_json) if draft.input_json else {}


def _item_or_raise(draft: AiDraft, project) -> TestItemRow:
    item_id = _input_payload(draft).get("item_id")
    item = db.session.get(TestItemRow, item_id) if item_id else None
    if item is None or item.project_id != project.id or item.deleted_at is not None:
        raise ApplyError("目标手顺行不存在或已删除")
    return item


def _apply_viewpoint(draft, reviewer, project, output) -> dict[str, Any]:
    module_id = str(output.get("module_id") or "")
    created_ids: list[int] = []
    for vp in output.get("viewpoints", []):
        purpose_parts = []
        if vp.get("precondition"):
            purpose_parts.append(f"前提：{vp['precondition']}")
        if vp.get("condition"):
            purpose_parts.append(f"条件：{vp['condition']}")
        values = {
            "test_id": vp.get("case_id") or "",
            "test_name": vp.get("title") or "",
            "viewpoint": _KIND_JP.get(vp.get("kind"), vp.get("kind") or ""),
            "purpose": "；".join(purpose_parts),
            "description": f"期待：{vp.get('expected', '')}",
            "remark": "[AI 生成观点草稿，待审核]" if draft else "",
            "traceability_id": module_id,
        }
        item = items_service.create_item(
            reviewer, project, values, draft=True, sheet="test", commit=False)
        created_ids.append(item.id)
    db.session.commit()
    return {"created_item_ids": created_ids, "module_id": module_id}


def _apply_procedure(draft, reviewer, project, output) -> dict[str, Any]:
    item = _item_or_raise(draft, project)
    steps_doc = output.get("steps_doc")
    if not isinstance(steps_doc, dict):
        raise ApplyError("草稿中没有可用的 steps_doc")
    items_service.update_item(
        reviewer, project, item, item.version,
        {"steps": json.dumps(steps_doc, ensure_ascii=False)})
    missing = output.get("missing_variables") or []
    return {"item_id": item.id,
            "missing_variables": missing,
            "note": ("missing_variables 请先走 sbs 场景补登记，"
                     "否则该手顺无法执行") if missing else ""}


def _apply_sbs(draft, reviewer, project, output) -> dict[str, Any]:
    payload = _input_payload(draft)
    model = db.session.get(ProjectModel, payload.get("model_id"))
    if model is None or model.project_id != project.id:
        raise ApplyError("目标 Silver 模型不存在")
    additions = output.get("sbs_additions") or ""
    base_content = (payload.get("current_sbs") or "").rstrip()
    content = (base_content + "\n\n" if base_content else "") + additions
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    revision = SbsRevision(
        project_id=project.id, model_id=model.id,
        filename=f"ai-draft-{draft.id}.sbs", content=content,
        sha256=sha, size=len(content.encode("utf-8")),
        author_id=reviewer.id,
    )
    db.session.add(revision)
    # Prune to the platform's 50-revision window per model, oldest first.
    old = (SbsRevision.query.filter_by(model_id=model.id)
           .order_by(SbsRevision.created_at.desc())
           .offset(50).all())
    for row in old:
        db.session.delete(row)
    db.session.commit()
    return {"sbs_revision_id": revision.id,
            "needed_variables": output.get("needed_variables") or []}


def _apply_lib(draft, reviewer, project, output) -> dict[str, Any]:
    lib_name = output.get("lib_name") or ""
    if not lib_name:
        raise ApplyError("草稿缺少 lib_name")
    values = {
        "lib_func": lib_name,
        "lib_name": lib_name,
        "lib_value": output.get("description") or "",
        "lib_para": json.dumps(output.get("lib_para") or [],
                               ensure_ascii=False),
        "lib_stb": json.dumps(output.get("lib_stb") or {}, ensure_ascii=False),
        "lib_note": "[AI 生成，人工提议触发]",
    }
    lib_row = items_service.create_item(
        reviewer, project, values, draft=True, sheet="lib", commit=False)
    rewritten_ids: list[int] = []
    for entry in output.get("rewritten") or []:
        item = db.session.get(TestItemRow, entry.get("item_id"))
        if item is None or item.project_id != project.id or item.deleted_at is not None:
            continue
        items_service.update_item(
            reviewer, project, item, item.version,
            {"steps": json.dumps(entry.get("steps_doc") or {},
                                 ensure_ascii=False)},
            commit=False)
        rewritten_ids.append(item.id)
    db.session.commit()
    return {"lib_item_id": lib_row.id, "rewritten_item_ids": rewritten_ids}


def _apply_failure(draft, reviewer, project, output) -> dict[str, Any]:
    item = _item_or_raise(draft, project)
    text = (
        f"[AI 差异分析 / {output.get('classification', '')}]\n"
        f"{output.get('analysis', '')}\n\n"
        f"最可能原因：{output.get('likely_cause', '')}\n"
        f"建议处理：{output.get('suggested_action', '')}"
    )
    comment = CellComment(
        project_id=project.id, test_item_id=item.id,
        field_key="ai_failure_analysis", content=text,
        created_by=reviewer.id,
    )
    db.session.add(comment)
    db.session.commit()
    return {"comment_id": comment.id, "item_id": item.id}
