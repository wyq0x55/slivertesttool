"""LAN Matrix route ownership: projects."""

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


def _collab_write_blocked(project_id) -> "Response | None":
    """Enforce the single-writer boundary (design doc §1.6 / §12.3).

    When ``COLLAB_REST_GUARD`` is enabled and the project is currently
    collaborative (a live CRDT room is heartbeating presence), the materializer
    is the single authoritative writer, so a direct REST row mutation would race
    it. Return a 409 response to reject such a write; return ``None`` to allow it.

    Default config disables the guard, so this is a no-op unless opted in. It
    also fails open: any presence-lookup error allows the write (never blocks
    editing because bookkeeping hiccuped).
    """
    if not current_app.config.get("COLLAB_REST_GUARD", False):
        return None
    try:
        from ...collab import presence
        if presence.is_collab_active(int(project_id)):
            return err(
                "COLLAB_ACTIVE",
                "该项目正在实时协同编辑，请在协同视图中修改（此改动已由协同层接管）。",
                status=409)
    except Exception:  # noqa: BLE001 - never block editing on a guard failure
        current_app.logger.debug("collab write-guard check failed", exc_info=True)
    return None

def _initials(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "?"
    # CJK: first char; latin: first letter of first two words.
    parts = name.split()
    if len(parts) >= 2 and parts[0][:1].isascii() and parts[0][:1].isalpha():
        return (parts[0][:1] + parts[1][:1]).upper()
    return name[:1].upper()


def _project_card_stats(project_ids: list[int]) -> dict[int, dict]:
    """Aggregate per-project task counts + member initials for the card grid.

    測試項 = total tasks, 通過率 = passed / total, 失敗 = failed count."""
    stats = {pid: {"task_total": 0, "task_passed": 0, "task_failed": 0,
                   "members": [], "member_extra": 0} for pid in project_ids}
    if not project_ids:
        return stats

    rows = (
        db.session.query(Task.project_id, Task.status, db.func.count(Task.id))
        .filter(Task.project_id.in_(project_ids))
        .group_by(Task.project_id, Task.status)
        .all()
    )
    for pid, status, count in rows:
        s = stats.get(pid)
        if s is None:
            continue
        s["task_total"] += count
        if status == TaskStatus.PASSED.value:
            s["task_passed"] += count
        elif status == TaskStatus.FAILED.value:
            s["task_failed"] += count

    members = (
        db.session.query(ProjectMember.project_id, LMUser.display_name, LMUser.username)
        .join(LMUser, LMUser.id == ProjectMember.user_id)
        .filter(ProjectMember.project_id.in_(project_ids))
        .order_by(ProjectMember.project_id, ProjectMember.id)
        .all()
    )
    for pid, display_name, username in members:
        s = stats.get(pid)
        if s is None:
            continue
        if len(s["members"]) < 4:
            s["members"].append(_initials(display_name or username))
        else:
            s["member_extra"] += 1
    return stats


@bp.get("/projects")
@login_required
def list_projects():
    projects = service.list_projects(g.user)
    stats = _project_card_stats([p.id for p in projects])
    payload = []
    for p in projects:
        d = p.to_dict()
        d.update(stats.get(p.id, {}))
        payload.append(d)
    return ok({"projects": payload})

@bp.post("/projects")
@login_required
def create_project():
    body = request.get_json(silent=True) or {}
    project = service.create_project(
        g.user, code=body.get("code", ""), name=body.get("name", ""),
        description=body.get("description", ""))
    return ok({"project": project.to_dict()}, status=201)

@bp.get("/projects/<int:project_id>")
@login_required
def get_project(project_id):
    project, _ = _project_and_role(project_id, "project.view")
    return ok({"project": project.to_dict(),
               "role": service.role_in_project(project.id, g.user)})

@bp.post("/projects/<int:project_id>/collab-token")
@login_required
def collab_token(project_id):
    """Mint a short-lived signed token for the real-time collaboration socket.

    Requires ``item.edit`` (only editors join the CRDT room; readers keep using
    the REST read path). The separate collab server verifies this token — signed
    with the shared ``SECRET_KEY`` — on connect. See design doc §8.
    """
    from ...collab import tokens
    project, role = _project_and_role(project_id, "item.edit")
    token = tokens.mint(
        current_app.config["SECRET_KEY"],
        user_id=g.user.id, username=g.user.username,
        project_id=project.id, role=role)
    return ok({
        "token": token,
        "room": fields.room_name(project.id),
        "expires_in": tokens.DEFAULT_MAX_AGE,
        # Optional explicit socket base (e.g. wss://host:1234); the frontend
        # falls back to deriving it from window.location when unset.
        "ws_url": current_app.config.get("COLLAB_WS_URL", ""),
    })

@bp.patch("/projects/<int:project_id>")
@login_required
def patch_project(project_id):
    project, _ = _project_and_role(project_id, "project.edit")
    body = request.get_json(silent=True) or {}
    project = service.update_project(g.user, project, body.get("changes", body))
    return ok({"project": project.to_dict()})

@bp.delete("/projects/<int:project_id>")
@login_required
def delete_project(project_id):
    project, _ = _project_and_role(project_id, "project.edit")
    counts = service.delete_project(g.user, project)
    return ok({"deleted": True, "removed": counts})

