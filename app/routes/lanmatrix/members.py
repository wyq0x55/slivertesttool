"""LAN Matrix route ownership: members."""

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


@bp.get("/projects/<int:project_id>/members")
@login_required
def list_members(project_id):
    _project_and_role(project_id, "project.view")
    members = service.list_members(project_id)
    return ok({"members": [m.to_dict() for m in members],
               "roles": list(service.PROJECT_ROLES)})

@bp.get("/projects/<int:project_id>/members/candidates")
@login_required
def member_candidates(project_id):
    _project_and_role(project_id, "project.members")
    existing = {m.user_id for m in service.list_members(project_id)}
    q = request.args.get("q", "")
    # With no query we present the full pick-list (all active users) so the
    # admin can choose anyone directly; a typed query narrows and stays snappy.
    users = service.search_users(q, limit=500 if not q.strip() else 50)
    out = [{"id": u.id, "username": u.username,
            "display_name": u.display_name or u.username}
           for u in users if u.id not in existing]
    return ok({"candidates": out})

@bp.post("/projects/<int:project_id>/members")
@login_required
def add_member(project_id):
    project, _ = _project_and_role(project_id, "project.members")
    body = request.get_json(silent=True) or {}
    member = service.add_member(
        g.user, project,
        username=(body.get("username") or "").strip(),
        user_id=body.get("user_id"),
        role=body.get("role", "reader"))
    return ok({"member": member.to_dict()}, status=201)

@bp.patch("/projects/<int:project_id>/members/<int:member_id>")
@login_required
def patch_member(project_id, member_id):
    project, _ = _project_and_role(project_id, "project.members")
    body = request.get_json(silent=True) or {}
    member = service.update_member_role(
        g.user, project, member_id, body.get("role", ""))
    return ok({"member": member.to_dict()})

@bp.delete("/projects/<int:project_id>/members/<int:member_id>")
@login_required
def remove_member(project_id, member_id):
    project, _ = _project_and_role(project_id, "project.members")
    service.remove_member(g.user, project, member_id)
    return ok({"removed": True})


# --------------------------------------------------------------------------- #
# Review sign-off
#
# Gated on ``item.review`` (project_admin / reviewer), which the permission
# matrix has always declared but nothing used until now.
# --------------------------------------------------------------------------- #
