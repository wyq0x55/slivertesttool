"""REST API blueprint ``/api/v1`` for the LAN Test Matrix (PRD §10).

All responses use the unified envelope. State-changing requests require a valid
session and a matching double-submit CSRF token (``X-CSRF-Token``). Permissions
are enforced server-side (the UI hiding buttons is not sufficient).
"""

from __future__ import annotations

import datetime as _dt
import functools
import io
import secrets
import uuid
import zipfile
from typing import Any, Optional

from flask import (
    Blueprint, Response, current_app, g, jsonify, request, send_file, session,
    stream_with_context,
)

from ..extensions import db
from ..services.lanmatrix import (
    dbadmin, excel_service, permissions, service, settings,
)
from ..models import DataJob, FieldDefinition, LMUser, Project, Task, TaskStatus
from ..services import (
    event_service, license_service, model_service, report_service,
    task_service, upload_service,
)
from ..services.upload_service import UploadError
from ..services.lanmatrix.permissions import PermissionDenied
from ..services.lanmatrix.service import ServiceError, VersionConflict

v1 = Blueprint("lanmatrix_api", __name__, url_prefix="/api/v1")

# Account-lockout policy is centrally configured via ``.env`` (LM_LOCK_*).
_LOCK_THRESHOLD = settings.LOCK_THRESHOLD
_LOCK_MINUTES = settings.LOCK_MINUTES


# --------------------------------------------------------------------------- #
# Envelope helpers
# --------------------------------------------------------------------------- #
def _request_id() -> str:
    rid = getattr(g, "request_id", None)
    if rid is None:
        rid = "req-" + uuid.uuid4().hex[:12]
        g.request_id = rid
    return rid


def ok(data: Any = None, status: int = 200):
    return jsonify(success=True, data=data, error=None, request_id=_request_id()), status


def err(code: str, message: str, *, details: Any = None, status: int = 400):
    return jsonify(
        success=False, data=None,
        error={"code": code, "message": message, "details": details},
        request_id=_request_id(),
    ), status


# --------------------------------------------------------------------------- #
# Auth plumbing
# --------------------------------------------------------------------------- #
def current_user() -> Optional[LMUser]:
    uid = session.get("lm_user_id")
    if uid is None:
        return None
    user = service.get_user(uid)
    if user is None or not user.is_active:
        return None
    return user


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return err("UNAUTHENTICATED", "未登录或会话已过期", status=401)
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def system_admin_required(fn):
    """Gate an endpoint to the bootstrap system administrator only."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return err("UNAUTHENTICATED", "未登录或会话已过期", status=401)
        if not user.is_system_admin:
            return err("PERMISSION_DENIED", "仅系统管理员可访问", status=403)
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


# Endpoints exempt from CSRF: login bootstraps the token, logout only clears
# state. Both are safe without a pre-existing token (login is guarded by
# credentials; logout is idempotent).
_CSRF_EXEMPT = {
    "lanmatrix_api.login", "lanmatrix_api.logout", "lanmatrix_api.register",
}


def _check_csrf() -> bool:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    if request.endpoint in _CSRF_EXEMPT:
        return True
    token = request.headers.get("X-CSRF-Token", "")
    return bool(token) and secrets.compare_digest(token, session.get("csrf_token", ""))


@v1.before_request
def _csrf_guard():
    if not _check_csrf():
        return err("CSRF_FAILED", "CSRF 校验失败", status=403)


def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()


def _project_and_role(project_id: int, capability: str):
    project = service.get_project(project_id)
    role = service.role_in_project(project.id, g.user)
    permissions.require(capability, role, is_system_admin=g.user.is_system_admin)
    return project, role


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
@v1.errorhandler(PermissionDenied)
def _perm(exc):
    return err("PERMISSION_DENIED", "没有该操作权限", details=str(exc), status=403)


@v1.errorhandler(VersionConflict)
def _conflict(exc):
    return err(exc.code, str(exc), details=exc.details, status=409)


@v1.errorhandler(ServiceError)
def _service_err(exc):
    status = {"NOT_FOUND": 404, "DUPLICATE": 409, "PERMISSION_DENIED": 403}.get(exc.code, 400)
    return err(exc.code, str(exc), details=exc.details, status=status)


# --------------------------------------------------------------------------- #
# Auth endpoints
# --------------------------------------------------------------------------- #
@v1.post("/auth/login")
def login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = service.get_user_by_name(username)
    now = _dt.datetime.now(_dt.timezone.utc)

    if user and user.locked_until and user.locked_until.replace(tzinfo=_dt.timezone.utc) > now:
        return err("ACCOUNT_LOCKED", "账号已被临时锁定，请稍后再试", status=423)
    if user is None or not user.is_active or not user.check_password(password):
        if user is not None:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= _LOCK_THRESHOLD:
                user.locked_until = now + _dt.timedelta(minutes=_LOCK_MINUTES)
                user.failed_logins = 0
            db.session.commit()
        return err("INVALID_CREDENTIALS", "用户名或密码错误", status=401)

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    db.session.commit()

    session.clear()
    session["lm_user_id"] = user.id
    session["csrf_token"] = secrets.token_hex(16)
    session.permanent = True
    return ok({"user": user.to_dict(), "csrf_token": session["csrf_token"]})


@v1.post("/auth/register")
def register():
    """Self-service registration for LAN users.

    Disabled unless ``LM_ALLOW_REGISTRATION`` is on. On success, an ``active``
    account is logged in immediately (session + CSRF token established, mirroring
    login); a ``disabled`` account (approval mode) is created in a pending state
    and the client is told to wait for an administrator.
    """
    if not settings.ALLOW_REGISTRATION:
        return err("REGISTRATION_DISABLED", "用户注册功能已关闭", status=403)

    body = request.get_json(silent=True) or {}
    user = service.register_user(
        (body.get("username") or "").strip(),
        body.get("password") or "",
        display_name=(body.get("display_name") or "").strip(),
        email=(body.get("email") or "").strip(),
    )

    if not user.is_active:
        return ok({"pending": True, "user": user.to_dict()}, status=201)

    session.clear()
    session["lm_user_id"] = user.id
    session["csrf_token"] = secrets.token_hex(16)
    session.permanent = True
    return ok(
        {"pending": False, "user": user.to_dict(),
         "csrf_token": session["csrf_token"]},
        status=201,
    )


@v1.post("/auth/logout")
def logout():
    session.clear()
    return ok({"logged_out": True})


@v1.get("/auth/me")
@login_required
def me():
    return ok({"user": g.user.to_dict(), "csrf_token": session.get("csrf_token")})


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
@v1.get("/projects")
@login_required
def list_projects():
    projects = service.list_projects(g.user)
    return ok({"projects": [p.to_dict() for p in projects]})


@v1.post("/projects")
@login_required
def create_project():
    body = request.get_json(silent=True) or {}
    project = service.create_project(
        g.user, code=body.get("code", ""), name=body.get("name", ""),
        description=body.get("description", ""))
    return ok({"project": project.to_dict()}, status=201)


@v1.get("/projects/<int:project_id>")
@login_required
def get_project(project_id):
    project, _ = _project_and_role(project_id, "project.view")
    return ok({"project": project.to_dict(),
               "role": service.role_in_project(project.id, g.user)})


@v1.post("/projects/<int:project_id>/collab-token")
@login_required
def collab_token(project_id):
    """Mint a short-lived signed token for the real-time collaboration socket.

    Requires ``item.edit`` (only editors join the CRDT room; readers keep using
    the REST read path). The separate collab server verifies this token — signed
    with the shared ``SECRET_KEY`` — on connect. See design doc §8.
    """
    from ..collab import tokens
    project, role = _project_and_role(project_id, "item.edit")
    token = tokens.mint(
        current_app.config["SECRET_KEY"],
        user_id=g.user.id, username=g.user.username,
        project_id=project.id, role=role)
    return ok({
        "token": token,
        "room": f"project:{project.id}",
        "expires_in": tokens.DEFAULT_MAX_AGE,
        # Optional explicit socket base (e.g. wss://host:1234); the frontend
        # falls back to deriving it from window.location when unset.
        "ws_url": current_app.config.get("COLLAB_WS_URL", ""),
    })


@v1.patch("/projects/<int:project_id>")
@login_required
def patch_project(project_id):
    project, _ = _project_and_role(project_id, "project.edit")
    body = request.get_json(silent=True) or {}
    project = service.update_project(g.user, project, body.get("changes", body))
    return ok({"project": project.to_dict()})


@v1.delete("/projects/<int:project_id>")
@login_required
def delete_project(project_id):
    project, _ = _project_and_role(project_id, "project.edit")
    counts = service.delete_project(g.user, project)
    return ok({"deleted": True, "removed": counts})


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #
@v1.get("/projects/<int:project_id>/fields")
@login_required
def list_fields(project_id):
    _project_and_role(project_id, "project.view")
    fields = service.list_fields(project_id)
    sheet = request.args.get("sheet")
    result = [f.to_dict() for f in fields]
    if sheet:
        result = [f for f in result if (f.get("sheet") or "test") == sheet]
    return ok({"fields": result})


@v1.post("/projects/<int:project_id>/fields")
@login_required
def add_field(project_id):
    project, _ = _project_and_role(project_id, "field.manage")
    body = request.get_json(silent=True) or {}
    fdef = service.add_field(g.user, project, body)
    return ok({"field": fdef.to_dict()}, status=201)


@v1.patch("/projects/<int:project_id>/fields/<int:field_id>")
@login_required
def patch_field(project_id, field_id):
    project, _ = _project_and_role(project_id, "field.manage")
    fdef = db.session.get(FieldDefinition, field_id)
    if fdef is None or fdef.project_id != project.id:
        return err("NOT_FOUND", "字段不存在", status=404)
    body = request.get_json(silent=True) or {}
    fdef = service.update_field(g.user, project, fdef, body.get("changes", body))
    return ok({"field": fdef.to_dict()})


@v1.delete("/projects/<int:project_id>/fields/<int:field_id>")
@login_required
def delete_field(project_id, field_id):
    project, _ = _project_and_role(project_id, "field.manage")
    fdef = db.session.get(FieldDefinition, field_id)
    if fdef is None or fdef.project_id != project.id:
        return err("NOT_FOUND", "字段不存在", status=404)
    service.delete_field(g.user, project, fdef)
    return ok({"deleted": field_id})


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #
@v1.get("/projects/<int:project_id>/items")
@login_required
def list_items(project_id):
    _project_and_role(project_id, "item.view")
    filters = request.args.get("filter")
    import json
    parsed_filters = json.loads(filters) if filters else []
    result = service.list_items(
        project_id,
        page=int(request.args.get("page", 1)),
        page_size=int(request.args.get("page_size", settings.PAGE_SIZE)),
        sort=request.args.get("sort"),
        filters=parsed_filters,
        combinator=request.args.get("combinator", "and"),
        quick=request.args.get("q"),
        sheet=request.args.get("sheet"),
    )
    return ok(result)


@v1.post("/projects/<int:project_id>/items")
@login_required
def create_item(project_id):
    project, _ = _project_and_role(project_id, "item.create")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    body = request.get_json(silent=True) or {}
    values = body.get("values", body)
    if not isinstance(values, dict):
        values = {}
    # ``draft`` may arrive at the top level or nested inside ``values``.
    draft = bool(body.get("draft", False) or values.get("draft", False))
    values = {k: v for k, v in values.items() if k != "draft"}
    # Optional positional insert (Excel-style "insert above / below" a row).
    anchor_id = body.get("anchor_id")
    place = body.get("place", "below")
    sheet = body.get("sheet") or values.get("sheet")
    item = service.create_item(g.user, project, values, draft=draft,
                               anchor_id=int(anchor_id) if anchor_id else None,
                               place="above" if place == "above" else "below",
                               sheet=sheet)
    return ok({"item": item.to_dict()}, status=201)


@v1.patch("/projects/<int:project_id>/items/<int:item_id>")
@login_required
def patch_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.edit")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    item = service.get_item(project_id, item_id)
    body = request.get_json(silent=True) or {}
    if "version" not in body:
        return err("VALIDATION_ERROR", "缺少版本号 version", status=400)
    item = service.update_item(g.user, project, item, int(body["version"]),
                               body.get("changes", {}))
    return ok({"item": item.to_dict()})


@v1.delete("/projects/<int:project_id>/items/<int:item_id>")
@login_required
def delete_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.delete")
    item = service.get_item(project_id, item_id)
    service.soft_delete_item(g.user, project, item)
    return ok({"deleted": True})


@v1.post("/projects/<int:project_id>/items/<int:item_id>/duplicate")
@login_required
def duplicate_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.create")
    item = service.get_item(project_id, item_id)
    dup = service.duplicate_item(g.user, project, item)
    return ok({"item": dup.to_dict()}, status=201)


@v1.post("/projects/<int:project_id>/items/<int:item_id>/restore")
@login_required
def restore_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.edit")
    item = service.restore_item(g.user, project, item_id)
    return ok({"item": item.to_dict()})


def _row_ids(body) -> list:
    ids = body.get("ids", [])
    if not isinstance(ids, list):
        return []
    out = []
    for x in ids:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


@v1.post("/projects/<int:project_id>/items/bulk-delete")
@login_required
def bulk_delete_items(project_id):
    project, _ = _project_and_role(project_id, "item.delete")
    body = request.get_json(silent=True) or {}
    deleted = service.bulk_soft_delete(g.user, project, _row_ids(body))
    return ok({"deleted": deleted})


@v1.post("/projects/<int:project_id>/items/bulk-duplicate")
@login_required
def bulk_duplicate_items(project_id):
    project, _ = _project_and_role(project_id, "item.create")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    body = request.get_json(silent=True) or {}
    created = service.bulk_duplicate(g.user, project, _row_ids(body))
    return ok({"items": [it.to_dict() for it in created],
               "created": len(created)}, status=201)


@v1.post("/projects/<int:project_id>/items/move")
@login_required
def move_items(project_id):
    project, _ = _project_and_role(project_id, "item.edit")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    body = request.get_json(silent=True) or {}
    direction = body.get("direction", "up")
    n = service.move_items(g.user, project, _row_ids(body), direction)
    return ok({"moved": n})


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #
@v1.post("/projects/<int:project_id>/items/batch-preview")
@login_required
def batch_preview(project_id):
    project, _ = _project_and_role(project_id, "item.batch")
    body = request.get_json(silent=True) or {}
    scope = body.get("scope", {})
    if scope.get("type") == "all":
        _project_and_role(project_id, "item.batch_all")
    result = service.batch_preview(project, body["field_key"], body["operation"], scope)
    return ok(result)


@v1.post("/projects/<int:project_id>/items/batch-update")
@login_required
def batch_update(project_id):
    project, _ = _project_and_role(project_id, "item.batch")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    body = request.get_json(silent=True) or {}
    scope = body.get("scope", {})
    if scope.get("type") == "all":
        _project_and_role(project_id, "item.batch_all")
    result = service.batch_update(g.user, project, body["field_key"],
                                  body["operation"], scope)
    return ok(result)


@v1.post("/projects/<int:project_id>/items/batch-undo")
@login_required
def batch_undo(project_id):
    project, _ = _project_and_role(project_id, "item.batch")
    body = request.get_json(silent=True) or {}
    result = service.batch_undo(g.user, project, body["batch_id"])
    return ok(result)


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #
@v1.get("/projects/<int:project_id>/items/<int:item_id>/comments")
@login_required
def list_comments(project_id, item_id):
    _project_and_role(project_id, "item.view")
    comments = service.list_comments(project_id, item_id)
    return ok({"comments": [c.to_dict() for c in comments]})


@v1.post("/projects/<int:project_id>/items/<int:item_id>/comments")
@login_required
def add_comment(project_id, item_id):
    project, _ = _project_and_role(project_id, "comment.add")
    item = service.get_item(project_id, item_id)
    body = request.get_json(silent=True) or {}
    c = service.add_comment(g.user, project, item,
                            body.get("field_key", ""), body.get("content", ""))
    return ok({"comment": c.to_dict()}, status=201)


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
@v1.get("/projects/<int:project_id>/excel/template")
@login_required
def excel_template(project_id):
    project, _ = _project_and_role(project_id, "export.run")
    buf = excel_service.build_template_bytes(project)
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@v1.post("/projects/<int:project_id>/imports")
@login_required
def create_import(project_id):
    project, _ = _project_and_role(project_id, "import.run")
    mode = request.form.get("mode", "upsert")
    if mode == "replace_all":
        _project_and_role(project_id, "import.replace")
    file = request.files.get("file")
    if file is None or not file.filename:
        return err("VALIDATION_ERROR", "未上传文件", status=400)
    if not file.filename.lower().endswith(".xlsx"):
        return err("VALIDATION_ERROR", "仅支持 .xlsx 文件", status=400)
    job = excel_service.create_import_preview(
        g.user, project, file.stream,
        original_filename=file.filename, mode=mode)
    return ok({"job": job.to_dict(with_preview=True)}, status=201)


@v1.get("/imports/<int:job_id>")
@login_required
def get_import(job_id):
    job = db.session.get(DataJob, job_id)
    if job is None or job.job_type != "import":
        return err("NOT_FOUND", "任务不存在", status=404)
    _project_and_role(job.project_id, "import.run")
    return ok({"job": job.to_dict(with_preview=True)})


@v1.post("/imports/<int:job_id>/commit")
@login_required
def commit_import(job_id):
    job = db.session.get(DataJob, job_id)
    if job is None or job.job_type != "import":
        return err("NOT_FOUND", "任务不存在", status=404)
    project, _ = _project_and_role(job.project_id, "import.run")
    result = excel_service.commit_import(g.user, project, job)
    return ok(result)


@v1.post("/projects/<int:project_id>/testmatrix/import")
@login_required
def import_test_matrix(project_id):
    """Import the fixed Japanese Test-Matrix workbook, mapping its columns onto
    the editor's Test-Matrix based fields (one-step: parse → create/update)."""
    from ..services.lanmatrix import testmatrix_bridge

    project, _ = _project_and_role(project_id, "import.run")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    mode = request.form.get("mode", "upsert")
    if mode == "replace_all":
        _project_and_role(project_id, "import.replace")
    file = request.files.get("file")
    if file is None or not file.filename:
        return err("VALIDATION_ERROR", "未上传文件", status=400)
    if not file.filename.lower().endswith(".xlsx"):
        return err("VALIDATION_ERROR", "仅支持 .xlsx 文件", status=400)
    try:
        summary = testmatrix_bridge.import_workbook(
            g.user, project, file.stream, mode=mode,
            original_filename=file.filename)
    except (ServiceError, PermissionDenied, VersionConflict):
        raise  # handled by the dedicated errorhandlers (return JSON + reason)
    except Exception as exc:  # noqa: BLE001 - never leak an opaque HTML 500
        current_app.logger.exception("Test-matrix import crashed")
        return err("IMPORT_PARSE_ERROR",
                   f"导入失败：{type(exc).__name__}: {exc}", status=400)
    return ok({"summary": summary}, status=201)


@v1.post("/projects/<int:project_id>/libfunc/import")
@login_required
def import_libfunc(project_id):
    """Import a Lib(Func) workbook: one function block -> one editor row
    (lib_* fields + shared step-detail JSON)."""
    from ..services.lanmatrix import libconst_bridge
    return _import_libconst(project_id, libconst_bridge.import_libfunc)


@v1.post("/projects/<int:project_id>/const/import")
@login_required
def import_const(project_id):
    """Import a Const workbook: one constant definition -> one editor row
    (const_* fields)."""
    from ..services.lanmatrix import libconst_bridge
    return _import_libconst(project_id, libconst_bridge.import_const)


def _import_libconst(project_id, importer):
    """Shared request handling for the Lib / Const one-step imports (mirrors the
    Test-Matrix import: parse -> create/update, with replace_all guarded by the
    ``import.replace`` permission)."""
    project, _ = _project_and_role(project_id, "import.run")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    mode = request.form.get("mode", "upsert")
    if mode == "replace_all":
        _project_and_role(project_id, "import.replace")
    file = request.files.get("file")
    if file is None or not file.filename:
        return err("VALIDATION_ERROR", "未上传文件", status=400)
    if not file.filename.lower().endswith(".xlsx"):
        return err("VALIDATION_ERROR", "仅支持 .xlsx 文件", status=400)
    try:
        summary = importer(g.user, project, file.stream, mode=mode,
                           original_filename=file.filename)
    except (ServiceError, PermissionDenied, VersionConflict):
        raise
    except Exception as exc:  # noqa: BLE001 - never leak an opaque HTML 500
        current_app.logger.exception("Lib/Const import crashed")
        return err("IMPORT_PARSE_ERROR",
                   f"导入失败：{type(exc).__name__}: {exc}", status=400)
    return ok({"summary": summary}, status=201)


@v1.get("/projects/<int:project_id>/testmatrix/export")
@login_required
def export_test_matrix(project_id):
    """Export the editor's items as a byte-compatible Japanese Test-Matrix
    workbook (summary sheet + per-category detail sheets)."""
    from ..services.lanmatrix import testmatrix_bridge

    project, _ = _project_and_role(project_id, "export.run")
    buf = testmatrix_bridge.export_workbook(project)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_test_matrix_{ts}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@v1.post("/projects/<int:project_id>/exports")
@login_required
def create_export(project_id):
    project, _ = _project_and_role(project_id, "export.run")
    body = request.get_json(silent=True) or {}
    buf = excel_service.export_project(
        project, columns=body.get("columns"), item_ids=body.get("item_ids"))
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_export_{ts}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
# Audit & health
# --------------------------------------------------------------------------- #
@v1.get("/projects/<int:project_id>/audit-logs")
@login_required
def audit_logs(project_id):
    _project_and_role(project_id, "audit.view")
    result = service.list_audit(
        project_id, page=int(request.args.get("page", 1)),
        page_size=int(request.args.get("page_size", settings.PAGE_SIZE)))
    return ok(result)


@v1.get("/health")
def health():
    db_ok = True
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    from .. import __version__
    return ok({
        "web": "ok",
        "database": "ok" if db_ok else "error",
        "version": __version__,
    })


# --------------------------------------------------------------------------- #
# Database administration (system_admin only) — PostgreSQL introspection + SQL
# --------------------------------------------------------------------------- #
@v1.get("/admin/db/overview")
@system_admin_required
def admin_db_overview():
    try:
        return ok(dbadmin.overview())
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=400)


@v1.post("/admin/db/query")
@system_admin_required
def admin_db_query():
    body = request.get_json(silent=True) or {}
    sql = (body.get("sql") or "").strip()
    if not sql:
        return err("VALIDATION_ERROR", "请输入 SQL 语句", status=400)
    read_only = bool(body.get("read_only", True))
    try:
        result = dbadmin.run_sql(sql, read_only=read_only)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok(result)


# --- No-SQL table browser + CRUD (system_admin only) ---------------------- #
@v1.get("/admin/db/tables")
@system_admin_required
def admin_db_tables():
    try:
        return ok({"tables": dbadmin.list_manageable_tables()})
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=400)


@v1.get("/admin/db/tables/<table>/schema")
@system_admin_required
def admin_db_table_schema(table):
    try:
        return ok(dbadmin.table_schema(table))
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=404)


@v1.get("/admin/db/tables/<table>/rows")
@system_admin_required
def admin_db_table_rows(table):
    try:
        result = dbadmin.read_rows(
            table,
            page=int(request.args.get("page", 1)),
            page_size=int(request.args.get("page_size", 50)),
            order_by=request.args.get("order_by") or None,
            descending=request.args.get("desc") in ("1", "true", "True"),
        )
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=400)
    return ok(result)


@v1.post("/admin/db/tables/<table>/rows")
@system_admin_required
def admin_db_insert_row(table):
    body = request.get_json(silent=True) or {}
    values = body.get("values", {})
    if not isinstance(values, dict):
        return err("VALIDATION_ERROR", "values 必须是对象", status=400)
    try:
        row = dbadmin.insert_row(table, values)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok({"row": row}, status=201)


@v1.patch("/admin/db/tables/<table>/rows")
@system_admin_required
def admin_db_update_row(table):
    body = request.get_json(silent=True) or {}
    pk = body.get("pk", {})
    changes = body.get("changes", {})
    if not isinstance(pk, dict) or not isinstance(changes, dict):
        return err("VALIDATION_ERROR", "pk 与 changes 必须是对象", status=400)
    try:
        row = dbadmin.update_row(table, pk, changes)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok({"row": row})


@v1.delete("/admin/db/tables/<table>/rows")
@system_admin_required
def admin_db_delete_row(table):
    body = request.get_json(silent=True) or {}
    pk = body.get("pk", {})
    if not isinstance(pk, dict):
        return err("VALIDATION_ERROR", "pk 必须是对象", status=400)
    try:
        deleted = dbadmin.delete_row(table, pk)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok({"deleted": deleted})


# --------------------------------------------------------------------------- #
# Project membership management
# --------------------------------------------------------------------------- #
@v1.get("/projects/<int:project_id>/members")
@login_required
def list_members(project_id):
    _project_and_role(project_id, "project.view")
    members = service.list_members(project_id)
    return ok({"members": [m.to_dict() for m in members],
               "roles": list(service.PROJECT_ROLES)})


@v1.get("/projects/<int:project_id>/members/candidates")
@login_required
def member_candidates(project_id):
    _project_and_role(project_id, "project.members")
    existing = {m.user_id for m in service.list_members(project_id)}
    users = service.search_users(request.args.get("q", ""), limit=20)
    out = [{"id": u.id, "username": u.username,
            "display_name": u.display_name or u.username}
           for u in users if u.id not in existing]
    return ok({"candidates": out})


@v1.post("/projects/<int:project_id>/members")
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


@v1.patch("/projects/<int:project_id>/members/<int:member_id>")
@login_required
def patch_member(project_id, member_id):
    project, _ = _project_and_role(project_id, "project.members")
    body = request.get_json(silent=True) or {}
    member = service.update_member_role(
        g.user, project, member_id, body.get("role", ""))
    return ok({"member": member.to_dict()})


@v1.delete("/projects/<int:project_id>/members/<int:member_id>")
@login_required
def remove_member(project_id, member_id):
    project, _ = _project_and_role(project_id, "project.members")
    service.remove_member(g.user, project, member_id)
    return ok({"removed": True})


# --------------------------------------------------------------------------- #
# Project Upload Tasks (test execution) — members only
# --------------------------------------------------------------------------- #
def _enqueue_task(task: Task) -> None:
    from ..jobqueue.tasks import run_task
    run_task(task.id)


def _form_items(files_field: str, paths_field: str):
    files = request.files.getlist(files_field)
    if not files:
        return []
    paths = request.form.getlist(paths_field)
    if len(paths) == len(files):
        return list(zip(paths, files))
    return [(f.filename, f) for f in files]


def _require_task(project_id: int, task_key: str, capability: str):
    project, _ = _project_and_role(project_id, capability)
    task = task_service.get_project_task(project_id, task_key)
    if task is None:
        raise ServiceError("任务不存在", code="NOT_FOUND")
    return project, task


@v1.get("/projects/<int:project_id>/tasks")
@login_required
def list_project_tasks(project_id):
    _, role = _project_and_role(project_id, "task.view")
    tasks = task_service.list_tasks(project_id=project_id, limit=1000)
    status = license_service.get_status()
    status["queued_jobs"] = Task.query.filter_by(
        project_id=project_id, status=TaskStatus.QUEUED.value).count()
    return ok({"tasks": [t.to_dict() for t in tasks],
               "models": model_service.list_models(),
               "license": status,
               "role": role,
               "can_delete": permissions.can("task.delete", role,
                                              is_system_admin=g.user.is_system_admin)})


@v1.post("/projects/<int:project_id>/tasks/upload-tree")
@login_required
def upload_project_tree(project_id):
    """Upload a test-case folder tree and queue the chosen test ids for a
    project. Only project members reach this (``task.upload``)."""
    from pathlib import Path

    import shutil

    from ..runners import run_layout

    project, _ = _project_and_role(project_id, "task.upload")
    cfg = current_app.config_obj
    if not model_service.has_models():
        return err("NO_MODEL", "尚未注册 .sil 模型，请联系系统管理员在管理台添加", status=409)

    model_name = (request.form.get("model") or "").strip()
    if not model_name:
        default = model_service.default_model()
        model_name = default["name"] if default else ""
    model_path = model_service.get_model_path(model_name)
    if model_path is None:
        return err("BAD_MODEL", "未知模型，请选择已注册的 .sil 模型", status=400)

    if not request.files.getlist("files"):
        return err("VALIDATION_ERROR", "未收到文件，请选择测试用例文件夹", status=400)

    try:
        info = upload_service.stage_tree(
            _form_items("files", "paths"),
            cfg.WORKSPACE_DIR,
            lib_items=_form_items("lib_files", "lib_paths"),
            stdlib_items=_form_items("stdlib_files", "stdlib_paths"),
        )
    except UploadError as exc:
        return err("UPLOAD_ERROR", str(exc), status=400)

    valid = set(info["test_ids"])
    selected = [t for t in request.form.getlist("test_ids") if t in valid]
    if not selected:
        upload_service.cleanup_staging(cfg.WORKSPACE_DIR, info["upload_key"])
        return err("VALIDATION_ERROR", "请至少选择一个 test id 提交", status=400,
                   details={"test_ids": info["test_ids"]})

    # Submitter identity comes from the authenticated account, not a free field.
    submitter = g.user.username
    folder_name = (request.form.get("folder_name") or "").strip() or "(folder upload)"
    sil_ref = str(Path(model_path).resolve())

    created, duplicates, errors = [], [], []
    try:
        for test_id in selected:
            existing = task_service.find_active_duplicate(
                submitter, test_id, project_id=project.id)
            if existing is not None:
                duplicates.append({"test_id": test_id, "task_id": existing.task_key})
                continue
            try:
                task = task_service.create_task(
                    task_name=test_id, file_name=folder_name,
                    submitter=submitter, test_id=test_id,
                    sil_relpath=sil_ref, sil_name=model_name, workspace="",
                    project_id=project.id, submitter_id=g.user.id)
                proj_root = run_layout.project_root(cfg, project)
                case_dir = run_layout.staging_dir(proj_root, test_id) / test_id
                shutil.rmtree(case_dir.parent, ignore_errors=True)
                upload_service.materialise_one(
                    cfg.WORKSPACE_DIR, info["upload_key"], case_dir, test_id)
                task.workspace = str(proj_root)
                db.session.commit()
                _enqueue_task(task)
                created.append({"test_id": test_id, "task_id": task.task_key})
            except UploadError as exc:
                errors.append({"test_id": test_id, "error": str(exc)})
    finally:
        upload_service.cleanup_staging(cfg.WORKSPACE_DIR, info["upload_key"])

    return ok({"created": created, "duplicates": duplicates,
               "errors": errors, "notes": info.get("notes", [])}, status=201)


@v1.post("/projects/<int:project_id>/tasks/run-selected")
@login_required
def run_selected_tasks(project_id):
    """Queue the chosen ``test`` rows for JSON-runner execution (no upload).

    The three runner inputs (``testcase_<id>.json`` / ``lib.json`` /
    ``constants.json``) are generated straight from the project's ``test`` /
    ``lib`` / ``const`` sheet rows, then the task is upserted by ``test_id``
    (a re-run overwrites the stored result) and enqueued.
    """
    import shutil
    from pathlib import Path

    from ..models import TestItemRow
    from ..runners import run_layout
    from ..services.lanmatrix import silver_json_export as sje

    project, _ = _project_and_role(project_id, "task.upload")
    cfg = current_app.config_obj
    if not model_service.has_models():
        return err("NO_MODEL", "尚未注册 .sil 模型，请联系系统管理员在管理台添加", status=409)

    body = request.get_json(silent=True) or {}
    raw_ids = body.get("test_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return err("VALIDATION_ERROR", "请至少选择一个 test id 提交", status=400)
    selected = [str(t).strip() for t in raw_ids if str(t).strip()]

    model_name = (body.get("model") or "").strip()
    if not model_name:
        default = model_service.default_model()
        model_name = default["name"] if default else ""
    model_path = model_service.get_model_path(model_name)
    if model_path is None:
        return err("BAD_MODEL", "未知模型，请选择已注册的 .sil 模型", status=400)
    sil_ref = str(Path(model_path).resolve())

    def _rows(sheet):
        return (TestItemRow.query
                .filter(TestItemRow.project_id == project.id,
                        TestItemRow.sheet == sheet,
                        TestItemRow.deleted_at.is_(None))
                .order_by(TestItemRow.row_order.asc(), TestItemRow.id.asc())
                .all())

    const_rows = _rows("const")
    lib_rows = _rows("lib")
    test_rows = _rows("test")
    by_test_id = {}
    for row in test_rows:
        tid = sje.row_test_id(row)
        if tid:
            by_test_id.setdefault(tid, row)

    submitter = g.user.username
    created, missing, errors = [], [], []
    for test_id in selected:
        row = by_test_id.get(test_id)
        if row is None:
            missing.append(test_id)
            continue
        try:
            task = task_service.upsert_task(
                task_name=test_id, file_name="(json runner)",
                submitter=submitter, test_id=test_id,
                sil_relpath=sil_ref, sil_name=model_name, workspace="",
                project_id=project.id, submitter_id=g.user.id)
            # Results are keyed by project + test_id (not the synthetic task
            # key). Run scripts are materialised into a short-lived staging dir;
            # the worker copies them into the runtime pool-instance dir and
            # deletes them once the run finishes.
            proj_root = run_layout.project_root(cfg, project)
            case_dir = run_layout.staging_dir(proj_root, test_id) / test_id
            shutil.rmtree(case_dir.parent, ignore_errors=True)
            sje.materialise_run_dir(case_dir, row, const_rows, lib_rows)
            task.workspace = str(proj_root)
            db.session.commit()
            _enqueue_task(task)
            created.append({"test_id": test_id, "task_id": task.task_key})
        except Exception as exc:  # noqa: BLE001 - surface per-row failure
            db.session.rollback()
            errors.append({"test_id": test_id, "error": str(exc)})

    return ok({"created": created, "missing": missing, "errors": errors},
              status=201)


@v1.get("/projects/<int:project_id>/tasks/<task_key>")
@login_required
def project_task_status(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.view")
    return ok({"task": task.to_dict()})


@v1.get("/projects/<int:project_id>/tasks/<task_key>/detail")
@login_required
def project_task_detail(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.view")
    events = event_service.fetch_since(task.id, 0, limit=5000)
    data = task.to_dict(detail=True)
    data["events"] = [e.to_dict() for e in events]
    return ok({"task": data})


@v1.get("/projects/<int:project_id>/tasks/<task_key>/stream")
@login_required
def project_task_stream(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.view")
    task_pk = task.id
    cfg = current_app.config_obj
    poll = cfg.SSE_POLL_SECONDS
    heartbeat = cfg.SSE_HEARTBEAT_SECONDS
    try:
        last_id = int(request.headers.get("Last-Event-ID")
                      or request.args.get("last_id") or 0)
    except (TypeError, ValueError):
        last_id = 0

    @stream_with_context
    def generate():
        nonlocal last_id
        import time
        since_beat = 0.0
        while True:
            db.session.expire_all()
            events = event_service.fetch_since(task_pk, last_id, limit=500)
            if events:
                for ev in events:
                    last_id = ev.id
                    yield event_service.format_sse(ev)
                since_beat = 0.0
            else:
                time.sleep(poll)
                since_beat += poll
                if since_beat >= heartbeat:
                    since_beat = 0.0
                    yield ": keep-alive\n\n"
            current = db.session.get(Task, task_pk)
            if current is not None and TaskStatus(current.status).is_final:
                tail = event_service.fetch_since(task_pk, last_id, limit=500)
                for ev in tail:
                    last_id = ev.id
                    yield event_service.format_sse(ev)
                yield "event: end\ndata: {}\n\n"
                return

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@v1.get("/projects/<int:project_id>/tasks/<task_key>/jdgrslt")
@login_required
def project_task_jdgrslt(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.view")
    path = report_service.jdgrslt_path(task)
    if path is None:
        return ok({"available": False, "verdict": task.result, "content": ""})
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ok({"available": False, "verdict": task.result, "content": "",
                   "message": f"无法读取 jdgrslt.log: {exc}"})
    from ..runners.test_runner import (
        count_failed_steps, extract_case_section, parse_verdict_text,
    )
    content = extract_case_section(raw, task.test_id)
    verdict = task.result
    if task.status in (TaskStatus.PASSED.value, TaskStatus.FAILED.value):
        verdict = parse_verdict_text(raw, task.test_id)
    return ok({"available": True, "verdict": verdict,
               "failed_steps": count_failed_steps(raw, task.test_id),
               "content": content})


@v1.post("/projects/<int:project_id>/tasks/<task_key>/cancel")
@login_required
def cancel_project_task(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.cancel")
    result = task_service.request_cancel(task)
    return ok({"task_id": task.task_key, "result": result, "message": task.message})


@v1.delete("/projects/<int:project_id>/tasks/<task_key>")
@login_required
def delete_project_task(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.delete")
    if not TaskStatus(task.status).is_final:
        task_service.request_cancel(task)
    workspace, test_id = task.workspace, task.test_id
    task_service.delete_task(task)
    _remove_task_dirs(workspace, test_id)
    return ok({"deleted": True})


def _remove_task_dirs(workspace: str, test_id: str) -> None:
    """Remove one test id's persistent results + any leftover staging.

    ``workspace`` is the shared per-project root, so only the test id's subtree
    is removed — never the whole project.
    """
    if not workspace or not test_id:
        return
    import shutil

    from ..runners import run_layout
    shutil.rmtree(run_layout.log_dir(workspace, test_id), ignore_errors=True)
    shutil.rmtree(run_layout.staging_dir(workspace, test_id), ignore_errors=True)


@v1.get("/projects/<int:project_id>/tasks/<task_key>/download")
@login_required
def download_project_task(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.download")
    buffer = report_service.build_report_stream(task)
    if buffer is None:
        return err("NOT_FOUND", "该任务暂无可下载的报告", status=404)
    from ..runners import run_layout
    # ``test_id`` is unique within a project, so it alone identifies a run's
    # results; no ``<task_key>_`` prefix is needed to disambiguate downloads.
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"{run_layout.safe_tid(task.test_id)}_report.zip")


def _parse_keys(raw) -> list[str]:
    """Normalise a ``keys`` payload (list and/or comma string) into a clean list."""
    items = [raw] if isinstance(raw, str) else list(raw or [])
    out = []
    for item in items:
        for key in str(item).split(","):
            key = key.strip()
            if key:
                out.append(key)
    return out


@v1.post("/projects/<int:project_id>/tasks/delete_batch")
@login_required
def delete_project_tasks_batch(project_id):
    """Delete several of a project's tasks (and their workspaces) at once.

    Body: ``{"keys": ["T000001", ...]}``. Requires ``task.delete``; each key is
    resolved within the project so cross-project deletion is impossible.
    """
    _project_and_role(project_id, "task.delete")
    body = request.get_json(silent=True) or {}
    keys = _parse_keys(body.get("keys"))
    results = []
    for key in keys:
        task = task_service.get_project_task(project_id, key)
        if task is None:
            results.append({"task_id": key, "result": "not_found"})
            continue
        if not TaskStatus(task.status).is_final:
            task_service.request_cancel(task)
        workspace, test_id = task.workspace, task.test_id
        task_service.delete_task(task)
        _remove_task_dirs(workspace, test_id)
        results.append({"task_id": key, "result": "deleted"})
    return ok({"results": results})


@v1.get("/projects/<int:project_id>/tasks/download_batch")
@login_required
def download_project_tasks_batch(project_id):
    """Bundle the reports of several of a project's tasks into one zip.

    Query: ``?keys=T000001,T000002`` (or repeated ``keys`` params). Requires
    ``task.download``; keys are resolved within the project.
    """
    project, _ = _project_and_role(project_id, "task.download")
    from ..runners import run_layout
    keys = _parse_keys(request.args.getlist("keys"))
    added = 0
    seen_folders: dict[str, int] = {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for key in keys:
            task = task_service.get_project_task(project_id, key)
            if task is None:
                continue
            # Compress each task's results on demand into its own folder, so the
            # bundle holds individual result files directly (no zip-in-zip).
            # ``test_id`` is unique per project, so it alone names the folder.
            folder = run_layout.safe_tid(task.test_id)
            dup = seen_folders.get(folder)
            if dup is not None:
                seen_folders[folder] = dup + 1
                folder = f"{folder}_{dup + 1}"
            else:
                seen_folders[folder] = 0
            if report_service.add_result_to_zip(bundle, task, arc_root=folder):
                added += 1
    if added == 0:
        return err("NOT_FOUND", "所选任务均无可下载的报告", status=404)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"{project.code}_reports_{added}.zip")


# --------------------------------------------------------------------------- #
# System-admin console (session-gated) — replaces the ADMIN_TOKEN admin page
# --------------------------------------------------------------------------- #
@v1.get("/admin/users")
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


@v1.post("/admin/users")
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


@v1.patch("/admin/users/<int:user_id>")
@system_admin_required
def admin_update_user(user_id):
    body = request.get_json(silent=True) or {}
    user = service.admin_update_user(g.user, user_id, body.get("changes", body))
    return ok({"user": user.to_dict()})


@v1.delete("/admin/users/<int:user_id>")
@system_admin_required
def admin_delete_user(user_id):
    service.admin_delete_user(g.user, user_id)
    return ok({"deleted": True})


@v1.get("/admin/models")
@system_admin_required
def admin_get_models():
    return ok({"models": model_service.list_models(include_path=True)})


@v1.post("/admin/models")
@system_admin_required
def admin_add_model():
    body = request.get_json(silent=True) or {}
    try:
        entry = model_service.add_model(body.get("name", ""), body.get("path", ""))
    except model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"model": entry,
               "models": model_service.list_models(include_path=True)}, status=201)


@v1.post("/admin/models/bulk")
@system_admin_required
def admin_bulk_models():
    body = request.get_json(silent=True) or {}
    try:
        result = model_service.replace_models(body.get("models") or [])
    except model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"models": result})


@v1.delete("/admin/models")
@system_admin_required
def admin_remove_model():
    body = request.get_json(silent=True) or {}
    removed = model_service.remove_model((body.get("name") or "").strip())
    return ok({"removed": removed,
               "models": model_service.list_models(include_path=True)})


@v1.get("/admin/license")
@system_admin_required
def admin_get_license():
    status = license_service.get_status()
    status["queued_jobs"] = Task.query.filter_by(
        status=TaskStatus.QUEUED.value).count()
    return ok({"license": status})


@v1.post("/admin/license")
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


@v1.get("/admin/tasks")
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


@v1.post("/admin/tasks/<task_key>/cancel")
@system_admin_required
def admin_cancel_task(task_key):
    task = task_service.get_by_key(task_key)
    if task is None:
        return err("NOT_FOUND", "任务不存在", status=404)
    result = task_service.request_cancel(task)
    return ok({"task_id": task.task_key, "result": result})


@v1.delete("/admin/tasks/<task_key>")
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
