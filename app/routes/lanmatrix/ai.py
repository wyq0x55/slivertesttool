"""AI agent endpoints (``/api/v1/ai/*``) — draft generation and review.

Flow: a project member POSTs a generation request; the scenario runs the
generate → validate → retry loop synchronously (bounded rounds) and the
result is stored as a pending ``AiDraft``. A reviewer then approves (apply
through the existing service layer) or rejects with a note. Humans only ever
review — they never transcribe.

Endpoints:

    GET  /api/v1/ai/settings              admin: effective AI config (key masked)
    PUT  /api/v1/ai/settings              admin: update api_base / api_key / model / timeout
    GET  /api/v1/ai/scenarios             scenario catalogue for the UI
    POST /api/v1/ai/drafts                {scenario, project_id, payload} → draft
    GET  /api/v1/ai/drafts                ?project_id&scenario&status
    GET  /api/v1/ai/drafts/<id>           full payload for the review dialog
    POST /api/v1/ai/drafts/<id>/approve   apply (editor+ permission)
    POST /api/v1/ai/drafts/<id>/reject    {note} (editor+ permission)
"""

from __future__ import annotations

import json

from flask import Blueprint, g, request

from ...extensions import db
from ...models.ai_draft import AiDraft
from ...services.ai import apply as ai_apply
from ...services.ai import config as ai_config
from ...services.ai import scenarios as ai_scenarios
from ...services.ai.base import GenerationError
from ...services.ai.provider import ProviderError
from ._base import (
    err, ok, register_common, system_admin_required, login_required,
    _project_and_role,
)

bp = Blueprint("lanmatrix_ai", __name__, url_prefix="/api/v1/ai")
register_common(bp)


def _require_edit(project_id: int):
    # ``item.edit`` covers draft generation and approval (both write project
    # content); reading drafts only needs project view.
    return _project_and_role(project_id, "item.edit")


@bp.get("/settings")
@system_admin_required
def get_settings():
    return ok(ai_config.get_ai_config())


@bp.put("/settings")
@system_admin_required
def put_settings():
    values = request.get_json(silent=True) or {}
    return ok(ai_config.update_ai_config(values))


@bp.get("/scenarios")
@login_required
def list_scenarios():
    return ok([{"name": name} for name in sorted(ai_scenarios.SCENARIOS)])


@bp.post("/drafts")
@login_required
def create_draft():
    body = request.get_json(silent=True) or {}
    scenario = str(body.get("scenario") or "").strip()
    project_id = body.get("project_id")
    payload = body.get("payload")
    if not scenario:
        return err("BAD_REQUEST", "scenario 不能为空", status=400)
    if not isinstance(project_id, int):
        return err("BAD_REQUEST", "project_id 必须是整数", status=400)
    if not isinstance(payload, dict):
        return err("BAD_REQUEST", "payload 必须是对象", status=400)
    if scenario not in ai_scenarios.SCENARIOS:
        return err("BAD_REQUEST",
                   f"未知场景 {scenario}，可选：{sorted(ai_scenarios.SCENARIOS)}",
                   status=400)
    if not ai_config.is_configured():
        return err("AI_NOT_CONFIGURED",
                   "AI 未配置：请管理员先设置 api_base / api_key / model", status=503)
    _require_edit(project_id)

    draft = AiDraft(project_id=project_id, scenario=scenario,
                    input_json=json.dumps(payload, ensure_ascii=False),
                    created_by=g.user.id)
    try:
        result = ai_scenarios.run_scenario(scenario, payload)
    except (ProviderError, GenerationError, ValueError) as exc:
        db.session.rollback()
        draft = AiDraft(project_id=project_id, scenario=scenario,
                        input_json=json.dumps(payload, ensure_ascii=False),
                        created_by=g.user.id,
                        status=AiDraft.STATUS_ERROR, error=str(exc))
        db.session.add(draft)
        db.session.commit()
        return err("AI_GENERATION_FAILED", str(exc),
                   details={"draft_id": draft.id}, status=502)
    draft.output_json = json.dumps(result.output, ensure_ascii=False, indent=2)
    draft.meta_json = json.dumps(
        {"model": result.model, "rounds": result.rounds, "log": result.log},
        ensure_ascii=False)
    db.session.add(draft)
    db.session.commit()
    return ok(draft.to_dict(), status=201)


@bp.get("/drafts")
@login_required
def list_drafts():
    from ._base import arg_int, arg_str
    project_id = arg_int("project_id", minimum=1)
    scenario = arg_str("scenario", max_length=24,
                       allowed=set(AiDraft.SCENARIOS))
    status = arg_str("status", max_length=16,
                     allowed={"pending", "approved", "rejected", "error"})
    if project_id is None:
        # Draft listing is always project-scoped: without this the filter
        # silently becomes "every project on the server".
        return err("BAD_REQUEST", "project_id 不能为空", status=400)
    _project_and_role(project_id, "project.view")
    query = AiDraft.query.filter_by(project_id=project_id)
    if scenario:
        query = query.filter_by(scenario=scenario)
    if status:
        query = query.filter_by(status=status)
    rows = (query.order_by(AiDraft.created_at.desc()).limit(200).all())
    return ok([d.to_dict(include_payload=False) for d in rows])


@bp.get("/drafts/<int:draft_id>")
@login_required
def get_draft(draft_id: int):
    draft = db.session.get(AiDraft, draft_id)
    if draft is None:
        return err("NOT_FOUND", "草稿不存在", status=404)
    _project_and_role(draft.project_id, "project.view")
    return ok(draft.to_dict())


@bp.post("/drafts/<int:draft_id>/approve")
@login_required
def approve_draft(draft_id: int):
    draft = db.session.get(AiDraft, draft_id)
    if draft is None:
        return err("NOT_FOUND", "草稿不存在", status=404)
    _require_edit(draft.project_id)
    try:
        result = ai_apply.apply_draft(draft, g.user)
    except ai_apply.ApplyError as exc:
        db.session.rollback()
        return err("APPLY_FAILED", str(exc), status=409)
    return ok({"draft_id": draft.id, "status": draft.status, "applied": result})


@bp.post("/drafts/<int:draft_id>/reject")
@login_required
def reject_draft(draft_id: int):
    draft = db.session.get(AiDraft, draft_id)
    if draft is None:
        return err("NOT_FOUND", "草稿不存在", status=404)
    _require_edit(draft.project_id)
    if draft.status not in (AiDraft.STATUS_PENDING, AiDraft.STATUS_ERROR):
        return err("BAD_REQUEST", f"草稿已处理（{draft.status}）", status=409)
    body = request.get_json(silent=True) or {}
    note = str(body.get("note") or "").strip()
    if not note:
        return err("BAD_REQUEST", "驳回必须填写原因（note）", status=400)
    draft.status = AiDraft.STATUS_REJECTED
    draft.review_note = note
    import datetime
    draft.reviewed_by = g.user.id
    draft.reviewed_at = datetime.datetime.utcnow()
    db.session.commit()
    return ok({"draft_id": draft.id, "status": draft.status})
