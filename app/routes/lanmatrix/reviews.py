"""LAN Matrix route ownership: reviews."""

from __future__ import annotations

import csv
import datetime as _dt
import io
import secrets
import zipfile
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, g, request, send_file, session,
    stream_with_context,
)

from ...extensions import db
from ...models import (
    DataJob, FieldDefinition, LMUser, Project, ProjectMember, Task, TaskStatus,
)
from ...services import (
    event_service, license_service, project_model_service,
    report_service, task_service, upload_service,
)
from ...services.upload_service import UploadError
from ...services.lanmatrix import (
    audit, dbadmin, excel_service, fields, permissions, sbs_service, service,
    settings, trash_service,
)
from ...services.lanmatrix.permissions import PermissionDenied
from ...services.lanmatrix.service import ServiceError, VersionConflict
from ._base import (
    ok, err, arg_int, arg_json, arg_str, arg_date,
    current_user, login_required, system_admin_required,
    register_common, _project_and_role, _client_ip,
    _LOCK_THRESHOLD, _LOCK_MINUTES,
)



from .projects_items import bp


def _review_rows(project_id: int, uuids: list[str]):
    """Load live rows by uuid, preserving the caller's order."""
    from ...models import TestItemRow

    wanted = [str(u) for u in uuids if str(u).strip()]
    if not wanted:
        return []
    found = (TestItemRow.query
             .filter_by(project_id=project_id, sheet="test", deleted_at=None)
             .filter(TestItemRow.uuid.in_(wanted))
             .all())
    by_uuid = {r.uuid: r for r in found}
    return [by_uuid[u] for u in wanted if u in by_uuid]


@bp.get("/projects/<int:project_id>/reviews")
@login_required
def list_project_reviews(project_id: int):
    """Pending reviews in one project, optionally only the caller's own queue."""
    from ...models import TestItemRow
    from ...services.lanmatrix import review_service

    _project_and_role(project_id, "item.review")

    # ``status`` defaults to pending (the historical behaviour) but accepts
    # approved / rejected / decided / all, because a decided review is the only
    # record that a verdict was ever challenged and it must stay reachable.
    status = (request.args.get("status") or review_service.PENDING).strip()
    if status not in review_service.STATUSES:
        status = review_service.PENDING

    q = (TestItemRow.query
         .filter_by(project_id=project_id, sheet="test", deleted_at=None)
         .filter(TestItemRow.review_status != review_service.NONE))
    if status == review_service.STATUS_DECIDED:
        q = q.filter(TestItemRow.review_status.in_(
            (review_service.APPROVED, review_service.REJECTED)))
    elif status != review_service.STATUS_ALL:
        q = q.filter(TestItemRow.review_status == status)
    if (request.args.get("mine") or "").strip() in ("1", "true", "yes"):
        q = q.filter(TestItemRow.reviewer_id == g.user.id)
    rows = q.order_by(TestItemRow.id.desc()).limit(500).all()

    ids = review_service.review_user_ids(rows)
    users = ({u.id: u for u in LMUser.query.filter(LMUser.id.in_(ids)).all()}
             if ids else {})
    return ok({
        "reviews": [review_service.row_review_dict(r, users) for r in rows],
        "counts": review_service.counts_for([project_id]).get(project_id, {}),
        "status": status,
    })


@bp.post("/projects/<int:project_id>/items/<row_uuid>/review")
@login_required
def review_item(project_id: int, row_uuid: str):
    """Approve or reject a single pending review."""
    from ...services.lanmatrix import review_service

    project, _ = _project_and_role(project_id, "item.review")
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        return err("INVALID_ARGUMENT", "action 必须是 approve 或 reject", status=400)

    rows = _review_rows(project_id, [row_uuid])
    if not rows:
        return err("NOT_FOUND", "用例不存在", status=404)

    try:
        row = review_service.decide(project, rows[0], action == "approve",
                                    actor_id=g.user.id,
                                    note=payload.get("note") or "")
    except review_service.ReviewError as exc:
        return err("INVALID_STATE", str(exc), status=400)

    db.session.commit()
    audit.record("item.review", actor_id=g.user.id, object_type="test_item",
                 object_id=row.uuid, project_id=project_id,
                 old_value=review_service.PENDING,
                 new_value={"status": row.review_status,
                            "verdict": row.review_verdict,
                            "note": row.review_note},
                 client_ip=_client_ip())
    db.session.commit()
    return ok({"review": review_service.row_review_dict(row)})


@bp.post("/projects/<int:project_id>/reviews/bulk")
@login_required
def review_items_bulk(project_id: int):
    """Approve/reject many rows at once.

    Only verdicts declared bulk-approvable are accepted; ``Untestable`` rows are
    reported back in ``skipped`` so the reviewer sees exactly what still needs an
    individual decision instead of silently believing the queue is empty.
    """
    from ...services.lanmatrix import review_service

    project, _ = _project_and_role(project_id, "item.review")
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        return err("INVALID_ARGUMENT", "action 必须是 approve 或 reject", status=400)
    uuids = payload.get("uuids")
    if not isinstance(uuids, list) or not uuids:
        return err("INVALID_ARGUMENT", "uuids 必须是非空数组", status=400)
    if len(uuids) > 500:
        return err("INVALID_ARGUMENT", "单次最多处理 500 条", status=400)

    rows = _review_rows(project_id, uuids)
    result = review_service.decide_bulk(project, rows, action == "approve",
                                        actor_id=g.user.id,
                                        note=payload.get("note") or "")
    audit.record("item.review.bulk", actor_id=g.user.id, object_type="project",
                 object_id=project_id, project_id=project_id,
                 new_value={"action": action,
                            "decided": len(result.get(
                                "approved" if action == "approve"
                                else "rejected", [])),
                            "skipped": len(result.get("skipped", []))},
                 client_ip=_client_ip())
    db.session.commit()
    return ok(result)


@bp.post("/projects/<int:project_id>/items/<row_uuid>/reviewer")
@login_required
def assign_item_reviewer(project_id: int, row_uuid: str):
    """Assign or clear the reviewer of a row."""
    from ...services.lanmatrix import review_service

    project, _ = _project_and_role(project_id, "item.review")
    payload = request.get_json(silent=True) or {}
    raw = payload.get("reviewer_id")
    reviewer_id = int(raw) if raw not in (None, "", 0) else None

    if reviewer_id is not None and not LMUser.query.get(reviewer_id):
        return err("NOT_FOUND", "指定的审核人不存在", status=404)

    rows = _review_rows(project_id, [row_uuid])
    if not rows:
        return err("NOT_FOUND", "用例不存在", status=404)

    review_service.assign_reviewer(rows[0], reviewer_id, project=project,
                                   actor_id=g.user.id)
    db.session.commit()
    return ok({"review": review_service.row_review_dict(rows[0])})


# --------------------------------------------------------------------------- #
# Scope exemption sign-off (項目作成 = 不要)
#
# Same gate as verdict review (``item.review``) and the same テスト区分 routing:
# whoever is trusted to judge a 区分's results is the person with the context to
# judge whether one of its cases may be skipped.
# --------------------------------------------------------------------------- #
@bp.get("/projects/<int:project_id>/exemptions")
@login_required
def list_project_exemptions(project_id: int):
    """Cases claiming 項目作成 = 不要, by decision state.

    Listing also routes freshly-typed claims (:func:`sync_pending`). Pending is
    derived, so this only stamps a reviewer and rings a bell -- a claim is in
    the queue whether or not this endpoint is ever called.
    """
    from ...services.lanmatrix import exemption_service

    project, _ = _project_and_role(project_id, "item.review")

    status = (request.args.get("status") or exemption_service.PENDING).strip()
    if status not in exemption_service.STATUSES:
        status = exemption_service.PENDING

    try:
        if exemption_service.sync_pending(project, actor_id=g.user.id):
            db.session.commit()
    except Exception:  # noqa: BLE001 - routing must never block the queue
        db.session.rollback()
        current_app.logger.warning("exemption sync failed for project %s",
                                   project_id, exc_info=True)

    reviewer_id = (g.user.id
                   if (request.args.get("mine") or "").strip() in ("1", "true", "yes")
                   else None)
    rows = exemption_service.queue_for([project_id], status=status,
                                       reviewer_id=reviewer_id)
    ids = exemption_service.review_user_ids(rows)
    users = ({u.id: u for u in LMUser.query.filter(LMUser.id.in_(ids)).all()}
             if ids else {})
    return ok({
        "exemptions": [exemption_service.row_dict(r, users) for r in rows],
        "counts": exemption_service.counts_for([project_id]).get(project_id, {}),
        "status": status,
    })


@bp.post("/projects/<int:project_id>/items/<row_uuid>/exemption")
@login_required
def decide_item_exemption(project_id: int, row_uuid: str):
    """Approve or reject one 不要 claim. A note is mandatory either way."""
    from ...services.lanmatrix import exemption_service

    project, _ = _project_and_role(project_id, "item.review")
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        return err("INVALID_ARGUMENT", "action 必须是 approve 或 reject", status=400)

    rows = _review_rows(project_id, [row_uuid])
    if not rows:
        return err("NOT_FOUND", "用例不存在", status=404)

    try:
        row = exemption_service.decide(project, rows[0], action == "approve",
                                       actor_id=g.user.id,
                                       note=payload.get("note") or "")
    except exemption_service.ExemptionError as exc:
        return err("INVALID_STATE", str(exc), status=400)

    db.session.commit()
    audit.record("item.exemption", actor_id=g.user.id, object_type="test_item",
                 object_id=row.uuid, project_id=project_id,
                 old_value=exemption_service.PENDING,
                 new_value={"status": row.exempt_status,
                            "note": row.exempt_note},
                 client_ip=_client_ip())
    db.session.commit()
    return ok({"exemption": exemption_service.row_dict(row)})


@bp.post("/projects/<int:project_id>/exemptions/bulk")
@login_required
def decide_exemptions_bulk(project_id: int):
    """Approve/reject many 不要 claims at once, with one shared reason.

    Bulk is allowed (unlike ``Untestable``) because 不要 is normally decided per
    feature area, but the note is still required and is written onto every row,
    so no approval is left without a justification.
    """
    from ...services.lanmatrix import exemption_service

    project, _ = _project_and_role(project_id, "item.review")
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        return err("INVALID_ARGUMENT", "action 必须是 approve 或 reject", status=400)
    uuids = payload.get("uuids")
    if not isinstance(uuids, list) or not uuids:
        return err("INVALID_ARGUMENT", "uuids 必须是非空数组", status=400)
    if len(uuids) > 500:
        return err("INVALID_ARGUMENT", "单次最多处理 500 条", status=400)
    if not (payload.get("note") or "").strip():
        return err("INVALID_ARGUMENT", "审批『不要』时必须填写理由", status=400)

    rows = _review_rows(project_id, uuids)
    result = exemption_service.decide_bulk(project, rows, action == "approve",
                                           actor_id=g.user.id,
                                           note=payload.get("note") or "")
    audit.record("item.exemption.bulk", actor_id=g.user.id,
                 object_type="project", object_id=project_id,
                 project_id=project_id,
                 new_value={"action": action,
                            "decided": len(result.get(
                                "approved" if action == "approve"
                                else "rejected", [])),
                            "skipped": len(result.get("skipped", []))},
                 client_ip=_client_ip())
    db.session.commit()
    return ok(result)


@bp.put("/projects/<int:project_id>/review_policy")
@login_required
def set_review_policy(project_id: int):
    """Set which verdicts require sign-off in this project.

    Project-level because it is a team policy, not a product decision: a
    safety-critical project reviews every PASS, an exploratory one reviews
    nothing.
    """
    from ...services.lanmatrix import review_routes as rr

    project, _ = _project_and_role(project_id, "project.edit")
    payload = request.get_json(silent=True) or {}
    previous = project.review_policy()
    prev_reviewer = project.default_reviewer_id
    prev_routes = project.review_route_rules()
    policy = {k: bool(payload.get(k, previous[k]))
              for k in Project.REVIEW_DEFAULTS}
    project.review_required_on = policy

    # Per-テスト区分 routing. Absent key means "leave as is", so a client that
    # only toggles a checkbox cannot wipe the routing table by omission.
    if "routes" in payload:
        raw_routes = payload.get("routes")
        if raw_routes in (None, ""):
            raw_routes = []
        if not isinstance(raw_routes, list):
            return err("INVALID_ARGUMENT", "区分审核人规则格式无效", status=400)
        if len(raw_routes) > rr.MAX_ROUTES:
            return err("INVALID_ARGUMENT",
                       f"区分审核人规则最多 {rr.MAX_ROUTES} 条", status=400)
        # Reject rather than silently drop malformed entries here: on the write
        # path a rule the user typed and cannot see afterwards is worse than an
        # error message. ``normalise_routes`` still runs last, so what is stored
        # is always canonical.
        cleaned: list[dict] = []
        for entry in raw_routes:
            if not isinstance(entry, dict):
                return err("INVALID_ARGUMENT", "区分审核人规则格式无效", status=400)
            category = str(entry.get("category") or "").strip()
            if not category:
                return err("INVALID_ARGUMENT", "区分不能为空", status=400)
            try:
                reviewer_id = int(entry.get("reviewer_id") or 0)
            except (TypeError, ValueError):
                return err("INVALID_ARGUMENT", "审核人 ID 无效", status=400)
            if reviewer_id <= 0:
                return err("INVALID_ARGUMENT",
                           f"区分「{category}」未指定审核人", status=400)
            if not _may_review(project, reviewer_id):
                return err("INVALID_ARGUMENT",
                           "审核人必须是本项目成员或项目负责人", status=400)
            cleaned.append({"category": category, "reviewer_id": reviewer_id})
        project.review_routes = rr.normalise_routes(cleaned)

    # The reviewer travels with the policy: turning review on without naming a
    # recipient is what produced a permanently empty queue before.
    if "default_reviewer_id" in payload:
        raw = payload.get("default_reviewer_id")
        if raw in (None, "", 0, "0"):
            project.default_reviewer_id = None
        else:
            try:
                reviewer_id = int(raw)
            except (TypeError, ValueError):
                return err("INVALID_ARGUMENT", "审核人 ID 无效", status=400)
            if not _may_review(project, reviewer_id):
                return err("INVALID_ARGUMENT",
                           "审核人必须是本项目成员或项目负责人", status=400)
            project.default_reviewer_id = reviewer_id

    db.session.commit()
    audit.record("project.review_policy", actor_id=g.user.id,
                 object_type="project", object_id=project_id,
                 project_id=project_id,
                 old_value={**previous, "default_reviewer_id": prev_reviewer,
                            "routes": prev_routes},
                 new_value={**policy,
                            "default_reviewer_id": project.default_reviewer_id,
                            "routes": project.review_route_rules()},
                 client_ip=_client_ip())
    db.session.commit()
    return ok({"review_required_on": project.review_policy(),
               "default_reviewer_id": project.default_reviewer_id,
               "review_routes": project.review_route_rules()})


def _may_review(project: Project, user_id: int) -> bool:
    """Whether ``user_id`` may be named as a reviewer of ``project``.

    Membership (or ownership) is the rule, so a routing rule cannot address
    somebody who then gets a 403 opening the case it sent them to.
    """
    if user_id == project.owner_id:
        return True
    return db.session.query(ProjectMember.id).filter(
        ProjectMember.project_id == project.id,
        ProjectMember.user_id == user_id,
    ).first() is not None


