"""AI agent endpoints (``/api/v1/ai/*``) — draft generation and review.

Flow: a project member POSTs a generation request; the draft is created in
``running`` and the scenario executes on the Huey worker (the two-phase
procedure batch can run for a minute — it must never sit inside an HTTP
timeout). The finished draft lands in ``pending`` for a reviewer, who then
approves (apply through the existing service layer, optionally only the
checked ``refs`` of a batch), edits inline before approving, or rejects with
a note. Humans only ever review — they never transcribe.

Endpoints:

    GET  /api/v1/ai/settings              admin: effective AI config (key masked)
    PUT  /api/v1/ai/settings              admin: update api_base / api_key / model / timeout
    GET  /api/v1/ai/scenarios             scenario catalogue for the UI
    POST /api/v1/ai/drafts                {scenario, project_id, payload} → draft (async)
    GET  /api/v1/ai/drafts                ?project_id&scenario&status
    GET  /api/v1/ai/drafts/<id>           full payload for the review dialog
    PUT  /api/v1/ai/drafts/<id>           {output} — inline edit before approving
    POST /api/v1/ai/drafts/<id>/approve   apply (editor+; optional {refs} for batch)
    POST /api/v1/ai/drafts/<id>/reject    {note} (editor+ permission)
    GET  /api/v1/ai/usage                 ?project_id&months — token usage stats
    GET  /api/v1/ai/signals               project signal dictionary
    PUT  /api/v1/ai/signals               {project_id, entries} — bulk replace
"""

from __future__ import annotations

import datetime
import json

from flask import Blueprint, g, request

from ...extensions import db
from ...models import AiDraft
from ...services.ai import apply as ai_apply
from ...services.ai import config as ai_config
from ...services.ai import scenarios as ai_scenarios
from ...services.ai import signal_dict as ai_signal_dict
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
                    created_by=g.user.id,
                    status=AiDraft.STATUS_RUNNING)
    db.session.add(draft)
    db.session.commit()
    try:
        from ...jobqueue.tasks import run_ai_generation
        run_ai_generation(draft.id)
    except (ProviderError, GenerationError, ValueError) as exc:
        # Immediate-mode (in-process) execution surfaces scenario failures
        # here; the queued path records the same state on the draft itself.
        db.session.rollback()
        draft = db.session.get(AiDraft, draft.id)
        if draft is not None and draft.status == AiDraft.STATUS_RUNNING:
            draft.status = AiDraft.STATUS_ERROR
            draft.error = str(exc)
            db.session.commit()
        return err("AI_GENERATION_FAILED", str(exc),
                   details={"draft_id": draft.id if draft else None},
                   status=502)
    except Exception as exc:  # noqa: BLE001 - the queue itself is unreachable
        db.session.rollback()
        draft = db.session.get(AiDraft, draft.id)
        if draft is not None:
            draft.status = AiDraft.STATUS_ERROR
            draft.error = f"任务队列不可用：{exc}"
            db.session.commit()
        return err("AI_QUEUE_UNAVAILABLE", f"任务队列不可用：{exc}",
                   details={"draft_id": draft.id if draft else None},
                   status=503)
    # The worker (immediate mode: this process) may already have finished the
    # generation — re-read so the response reflects the real state.
    db.session.expire_all()
    draft = db.session.get(AiDraft, draft.id)
    return ok(draft.to_dict(), status=201)


@bp.get("/drafts")
@login_required
def list_drafts():
    from ._base import arg_int, arg_str
    project_id = arg_int("project_id", minimum=1)
    scenario = arg_str("scenario", max_length=24,
                       allowed=set(AiDraft.SCENARIOS))
    status = arg_str("status", max_length=16,
                     allowed={"running", "pending", "approved", "rejected",
                              "error"})
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


@bp.put("/drafts/<int:draft_id>")
@login_required
def update_draft(draft_id: int):
    """Inline edit: fix the last 5% of an output, then approve as a whole."""
    draft = db.session.get(AiDraft, draft_id)
    if draft is None:
        return err("NOT_FOUND", "草稿不存在", status=404)
    _require_edit(draft.project_id)
    if draft.status not in (AiDraft.STATUS_PENDING, AiDraft.STATUS_ERROR):
        return err("BAD_REQUEST", f"草稿当前状态（{draft.status}）不可编辑",
                   status=409)
    body = request.get_json(silent=True) or {}
    output = body.get("output")
    if not isinstance(output, dict):
        return err("BAD_REQUEST", "output 必须是对象", status=400)
    draft.output_json = json.dumps(output, ensure_ascii=False, indent=2)
    meta = json.loads(draft.meta_json) if draft.meta_json else {}
    meta["edited"] = True
    draft.meta_json = json.dumps(meta, ensure_ascii=False)
    db.session.commit()
    return ok(draft.to_dict())


@bp.post("/drafts/<int:draft_id>/approve")
@login_required
def approve_draft(draft_id: int):
    draft = db.session.get(AiDraft, draft_id)
    if draft is None:
        return err("NOT_FOUND", "草稿不存在", status=404)
    _require_edit(draft.project_id)
    body = request.get_json(silent=True) or {}
    refs = body.get("refs")
    if refs is not None and (not isinstance(refs, list)
                             or not all(isinstance(r, str) for r in refs)):
        return err("BAD_REQUEST", "refs 必须是字符串数组", status=400)
    if not refs and draft.status == AiDraft.STATUS_RUNNING:
        return err("BAD_REQUEST", "草稿仍在生成中，请稍候", status=409)
    try:
        result = ai_apply.apply_draft(draft, g.user, refs=refs)
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
    draft.reviewed_by = g.user.id
    draft.reviewed_at = datetime.datetime.utcnow()
    db.session.commit()
    return ok({"draft_id": draft.id, "status": draft.status})


@bp.get("/usage")
@login_required
def usage_stats():
    """Token usage per project — aggregated from each draft's meta.usage."""
    from ._base import arg_int
    project_id = arg_int("project_id", minimum=1)
    months = arg_int("months", minimum=1) or 3
    months = min(months, 12)
    if project_id is None:
        return err("BAD_REQUEST", "project_id 不能为空", status=400)
    _project_and_role(project_id, "project.view")
    since = datetime.datetime.utcnow() - datetime.timedelta(days=30 * months)
    rows = (AiDraft.query
            .filter(AiDraft.project_id == project_id,
                    AiDraft.created_at >= since)
            .order_by(AiDraft.created_at.desc()).limit(1000).all())

    totals = {"input_tokens": 0, "output_tokens": 0, "drafts": len(rows)}
    per_scenario: dict[str, dict] = {}
    per_month: dict[str, dict] = {}
    for d in rows:
        try:
            meta = json.loads(d.meta_json) if d.meta_json else {}
        except ValueError:
            meta = {}
        usage = meta.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        totals["input_tokens"] += in_tok
        totals["output_tokens"] += out_tok
        bucket = per_scenario.setdefault(
            d.scenario, {"count": 0, "input_tokens": 0, "output_tokens": 0})
        bucket["count"] += 1
        bucket["input_tokens"] += in_tok
        bucket["output_tokens"] += out_tok
        month = (d.created_at or datetime.datetime.utcnow()).strftime("%Y-%m")
        mb = per_month.setdefault(
            month, {"count": 0, "input_tokens": 0, "output_tokens": 0})
        mb["count"] += 1
        mb["input_tokens"] += in_tok
        mb["output_tokens"] += out_tok
    return ok({"months": months, "totals": totals,
               "per_scenario": per_scenario, "per_month": per_month})


@bp.get("/signals")
@login_required
def list_signals():
    from ._base import arg_int
    project_id = arg_int("project_id", minimum=1)
    if project_id is None:
        return err("BAD_REQUEST", "project_id 不能为空", status=400)
    _project_and_role(project_id, "project.view")
    return ok(ai_signal_dict.entries_for(project_id))


@bp.put("/signals")
@login_required
def put_signals():
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id")
    entries = body.get("entries")
    if not isinstance(project_id, int):
        return err("BAD_REQUEST", "project_id 必须是整数", status=400)
    if not isinstance(entries, list):
        return err("BAD_REQUEST", "entries 必须是数组", status=400)
    _require_edit(project_id)
    try:
        count = ai_signal_dict.replace_entries(project_id, g.user.id, entries)
    except ValueError as exc:
        db.session.rollback()
        return err("BAD_REQUEST", str(exc), status=400)
    return ok({"count": count, "entries": ai_signal_dict.entries_for(project_id)})
