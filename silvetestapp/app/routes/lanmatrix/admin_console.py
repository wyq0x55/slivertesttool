"""System-admin console (users, models, license, tasks) for the LAN Test Matrix API."""

from __future__ import annotations

import datetime as _dt
import io
import json
import secrets
import zipfile
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, g, request, send_file, session,
    stream_with_context,
)

from ...extensions import db
from ...models import DataJob, FieldDefinition, LMUser, Project, Task, TaskStatus
from ...services import (
    event_service, license_service, model_service, report_service,
    task_service, upload_service,
)
from ...services.upload_service import UploadError
from ...services.lanmatrix import (
    dbadmin, excel_service, permissions, service, settings,
)
from ._base import (
    ok, err, current_user, login_required, system_admin_required,
    register_common, _project_and_role, _client_ip,
    _LOCK_THRESHOLD, _LOCK_MINUTES,
)
# ``admin_delete_task`` reuses the task-workspace cleanup helper that lives on
# the tasks blueprint (both delete tasks + their on-disk staging/log dirs).
from .tasks import _remove_task_dirs

bp = Blueprint("lanmatrix_admin_console", __name__, url_prefix="/api/v1")
register_common(bp)

@bp.get("/admin/users")
@system_admin_required
def admin_list_users():
    users = service.list_users()
    out = []
    for u in users:
        d = u.to_dict()
        d["is_active"] = u.is_active
        d["project_count"] = service.user_project_count(u.id)
        out.append(d)
    return ok({"users": out})

@bp.post("/admin/users")
@system_admin_required
def admin_create_user():
    body = request.get_json(silent=True) or {}
    user = service.admin_create_user(
        g.user,
        username=body.get("username", ""),
        password=body.get("password", ""),
        display_name=body.get("display_name", ""),
        email=body.get("email", ""),
        is_system_admin=bool(body.get("is_system_admin", False)),
        status=body.get("status", "active"))
    return ok({"user": user.to_dict()}, status=201)

@bp.patch("/admin/users/<int:user_id>")
@system_admin_required
def admin_update_user(user_id):
    body = request.get_json(silent=True) or {}
    user = service.admin_update_user(g.user, user_id, body.get("changes", body))
    return ok({"user": user.to_dict()})

@bp.delete("/admin/users/<int:user_id>")
@system_admin_required
def admin_delete_user(user_id):
    service.admin_delete_user(g.user, user_id)
    return ok({"deleted": True})

@bp.get("/admin/models")
@system_admin_required
def admin_get_models():
    return ok({"models": model_service.list_models(include_path=True)})

@bp.post("/admin/models")
@system_admin_required
def admin_add_model():
    body = request.get_json(silent=True) or {}
    try:
        entry = model_service.add_model(body.get("name", ""), body.get("path", ""))
    except model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"model": entry,
               "models": model_service.list_models(include_path=True)}, status=201)

@bp.post("/admin/models/bulk")
@system_admin_required
def admin_bulk_models():
    body = request.get_json(silent=True) or {}
    try:
        result = model_service.replace_models(body.get("models") or [])
    except model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"models": result})

@bp.delete("/admin/models")
@system_admin_required
def admin_remove_model():
    body = request.get_json(silent=True) or {}
    removed = model_service.remove_model((body.get("name") or "").strip())
    return ok({"removed": removed,
               "models": model_service.list_models(include_path=True)})

@bp.get("/admin/license")
@system_admin_required
def admin_get_license():
    status = license_service.get_status()
    status["queued_jobs"] = Task.query.filter_by(
        status=TaskStatus.QUEUED.value).count()
    return ok({"license": status})

@bp.post("/admin/license")
@system_admin_required
def admin_set_license():
    body = request.get_json(silent=True) or {}
    try:
        count = int(body.get("count"))
    except (TypeError, ValueError):
        return err("VALIDATION_ERROR", "count 必须是 >= 1 的整数", status=400)
    try:
        applied = license_service.set_limit(count)
    except ValueError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"count": applied})

@bp.get("/admin/tasks")
@system_admin_required
def admin_list_tasks():
    tasks = task_service.list_tasks(limit=1000)
    # Attach project code for the console table.
    proj_codes = {p.id: p.code for p in Project.query.all()}
    out = []
    for t in tasks:
        d = t.to_dict()
        d["project_code"] = proj_codes.get(t.project_id)
        out.append(d)
    return ok({"tasks": out})

@bp.post("/admin/tasks/<task_key>/cancel")
@system_admin_required
def admin_cancel_task(task_key):
    task = task_service.get_by_key(task_key)
    if task is None:
        return err("NOT_FOUND", "任务不存在", status=404)
    result = task_service.request_cancel(task)
    return ok({"task_id": task.task_key, "result": result})

@bp.delete("/admin/tasks/<task_key>")
@system_admin_required
def admin_delete_task(task_key):
    task = task_service.get_by_key(task_key)
    if task is None:
        return err("NOT_FOUND", "任务不存在", status=404)
    if not TaskStatus(task.status).is_final:
        task_service.request_cancel(task)
    workspace, test_id = task.workspace, task.test_id
    task_service.delete_task(task)
    _remove_task_dirs(workspace, test_id)
    return ok({"deleted": True})
