"""Project / field / item CRUD, batch operations, comments, Excel import-export, audit logs and membership for the LAN Test Matrix API."""

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
    event_service, license_service, project_model_service,
    report_service, task_service, upload_service,
)
from ...services.upload_service import UploadError
from ...services.lanmatrix import (
    dbadmin, excel_service, fields, permissions, service, settings,
)
from ...services.lanmatrix.permissions import PermissionDenied
from ...services.lanmatrix.service import ServiceError, VersionConflict
from ._base import (
    ok, err, current_user, login_required, system_admin_required,
    register_common, _project_and_role, _client_ip,
    _LOCK_THRESHOLD, _LOCK_MINUTES,
)

bp = Blueprint("lanmatrix_projects", __name__, url_prefix="/api/v1")
register_common(bp)


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

@bp.get("/projects")
@login_required
def list_projects():
    projects = service.list_projects(g.user)
    return ok({"projects": [p.to_dict() for p in projects]})

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

@bp.get("/projects/<int:project_id>/fields")
@login_required
def list_fields(project_id):
    _project_and_role(project_id, "project.view")
    fields = service.list_fields(project_id)
    sheet = request.args.get("sheet")
    result = [f.to_dict() for f in fields]
    if sheet:
        result = [f for f in result if (f.get("sheet") or "test") == sheet]
    return ok({"fields": result})

@bp.post("/projects/<int:project_id>/fields")
@login_required
def add_field(project_id):
    project, _ = _project_and_role(project_id, "field.manage")
    body = request.get_json(silent=True) or {}
    fdef = service.add_field(g.user, project, body)
    return ok({"field": fdef.to_dict()}, status=201)

@bp.patch("/projects/<int:project_id>/fields/<int:field_id>")
@login_required
def patch_field(project_id, field_id):
    project, _ = _project_and_role(project_id, "field.manage")
    fdef = db.session.get(FieldDefinition, field_id)
    if fdef is None or fdef.project_id != project.id:
        return err("NOT_FOUND", "字段不存在", status=404)
    body = request.get_json(silent=True) or {}
    fdef = service.update_field(g.user, project, fdef, body.get("changes", body))
    return ok({"field": fdef.to_dict()})

@bp.delete("/projects/<int:project_id>/fields/<int:field_id>")
@login_required
def delete_field(project_id, field_id):
    project, _ = _project_and_role(project_id, "field.manage")
    fdef = db.session.get(FieldDefinition, field_id)
    if fdef is None or fdef.project_id != project.id:
        return err("NOT_FOUND", "字段不存在", status=404)
    service.delete_field(g.user, project, fdef)
    return ok({"deleted": field_id})

# --------------------------------------------------------------------------- #
# Per-project plant models (.sil path registration + dll/sbs bundle upload)
# --------------------------------------------------------------------------- #
@bp.get("/projects/<int:project_id>/models")
@login_required
def list_project_models(project_id):
    _, role = _project_and_role(project_id, "project.view")
    can_manage = permissions.can("model.manage", role,
                                 is_system_admin=g.user.is_system_admin)
    return ok({"models": project_model_service.list_models(
                   project_id, include_path=can_manage),
               "can_manage": can_manage})

@bp.post("/projects/<int:project_id>/models")
@login_required
def add_project_model(project_id):
    _project_and_role(project_id, "model.manage")
    body = request.get_json(silent=True) or {}
    try:
        entry = project_model_service.add_path_model(
            project_id, body.get("name", ""), body.get("path", ""),
            created_by=g.user.id)
    except project_model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"model": entry,
               "models": project_model_service.list_models(
                   project_id, include_path=True)}, status=201)

@bp.post("/projects/<int:project_id>/models/upload")
@login_required
def upload_project_model(project_id):
    _project_and_role(project_id, "model.manage")
    dll = request.files.get("dll")
    sbs = request.files.get("sbs")
    pdb = request.files.get("pdb")
    if dll is None or sbs is None or pdb is None:
        return err("VALIDATION_ERROR", "请同时上传 dll、sbs 与 pdb 文件", status=400)
    try:
        entry = project_model_service.add_bundle_model(
            project_id, request.form.get("name", ""), dll, sbs,
            current_app.config_obj, pdb=pdb, created_by=g.user.id)
    except project_model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"model": entry,
               "models": project_model_service.list_models(
                   project_id, include_path=True)}, status=201)

@bp.delete("/projects/<int:project_id>/models")
@login_required
def remove_project_model(project_id):
    _project_and_role(project_id, "model.manage")
    body = request.get_json(silent=True) or {}
    removed = project_model_service.remove_model(
        project_id, (body.get("name") or "").strip())
    return ok({"removed": removed,
               "models": project_model_service.list_models(
                   project_id, include_path=True)})

@bp.get("/projects/<int:project_id>/items")
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

@bp.post("/projects/<int:project_id>/items")
@login_required
def create_item(project_id):
    project, _ = _project_and_role(project_id, "item.create")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
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

@bp.patch("/projects/<int:project_id>/items/<int:item_id>")
@login_required
def patch_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.edit")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    item = service.get_item(project_id, item_id)
    body = request.get_json(silent=True) or {}
    if "version" not in body:
        return err("VALIDATION_ERROR", "缺少版本号 version", status=400)
    item = service.update_item(g.user, project, item, int(body["version"]),
                               body.get("changes", {}))
    return ok({"item": item.to_dict()})

@bp.delete("/projects/<int:project_id>/items/<int:item_id>")
@login_required
def delete_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.delete")
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    item = service.get_item(project_id, item_id)
    service.soft_delete_item(g.user, project, item)
    return ok({"deleted": True})

@bp.post("/projects/<int:project_id>/items/<int:item_id>/duplicate")
@login_required
def duplicate_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.create")
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    item = service.get_item(project_id, item_id)
    dup = service.duplicate_item(g.user, project, item)
    return ok({"item": dup.to_dict()}, status=201)

@bp.post("/projects/<int:project_id>/items/<int:item_id>/restore")
@login_required
def restore_item(project_id, item_id):
    project, _ = _project_and_role(project_id, "item.edit")
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
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

@bp.post("/projects/<int:project_id>/items/bulk-delete")
@login_required
def bulk_delete_items(project_id):
    project, _ = _project_and_role(project_id, "item.delete")
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    deleted = service.bulk_soft_delete(g.user, project, _row_ids(body))
    return ok({"deleted": deleted})

@bp.post("/projects/<int:project_id>/items/bulk-duplicate")
@login_required
def bulk_duplicate_items(project_id):
    project, _ = _project_and_role(project_id, "item.create")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    created = service.bulk_duplicate(g.user, project, _row_ids(body))
    return ok({"items": [it.to_dict() for it in created],
               "created": len(created)}, status=201)

@bp.post("/projects/<int:project_id>/items/move")
@login_required
def move_items(project_id):
    project, _ = _project_and_role(project_id, "item.edit")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    # move_items rewrites the whole sheet's row_order; under collaboration the
    # Y.Array index is authoritative, so this path must never run (design §12.3).
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    direction = body.get("direction", "up")
    n = service.move_items(g.user, project, _row_ids(body), direction)
    return ok({"moved": n})

@bp.post("/projects/<int:project_id>/items/batch-preview")
@login_required
def batch_preview(project_id):
    project, _ = _project_and_role(project_id, "item.batch")
    body = request.get_json(silent=True) or {}
    scope = body.get("scope", {})
    if scope.get("type") == "all":
        _project_and_role(project_id, "item.batch_all")
    result = service.batch_preview(project, body["field_key"], body["operation"], scope)
    return ok(result)

@bp.post("/projects/<int:project_id>/items/batch-update")
@login_required
def batch_update(project_id):
    project, _ = _project_and_role(project_id, "item.batch")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    scope = body.get("scope", {})
    if scope.get("type") == "all":
        _project_and_role(project_id, "item.batch_all")
    result = service.batch_update(g.user, project, body["field_key"],
                                  body["operation"], scope)
    return ok(result)

@bp.post("/projects/<int:project_id>/items/batch-undo")
@login_required
def batch_undo(project_id):
    project, _ = _project_and_role(project_id, "item.batch")
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    result = service.batch_undo(g.user, project, body["batch_id"])
    return ok(result)

@bp.get("/projects/<int:project_id>/items/<int:item_id>/comments")
@login_required
def list_comments(project_id, item_id):
    _project_and_role(project_id, "item.view")
    comments = service.list_comments(project_id, item_id)
    return ok({"comments": [c.to_dict() for c in comments]})

@bp.post("/projects/<int:project_id>/items/<int:item_id>/comments")
@login_required
def add_comment(project_id, item_id):
    project, _ = _project_and_role(project_id, "comment.add")
    item = service.get_item(project_id, item_id)
    body = request.get_json(silent=True) or {}
    c = service.add_comment(g.user, project, item,
                            body.get("field_key", ""), body.get("content", ""))
    return ok({"comment": c.to_dict()}, status=201)

@bp.get("/projects/<int:project_id>/excel/template")
@login_required
def excel_template(project_id):
    project, _ = _project_and_role(project_id, "export.run")
    buf = excel_service.build_template_bytes(project)
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@bp.post("/projects/<int:project_id>/imports")
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

@bp.get("/imports/<int:job_id>")
@login_required
def get_import(job_id):
    job = db.session.get(DataJob, job_id)
    if job is None or job.job_type != "import":
        return err("NOT_FOUND", "任务不存在", status=404)
    _project_and_role(job.project_id, "import.run")
    return ok({"job": job.to_dict(with_preview=True)})

@bp.post("/imports/<int:job_id>/commit")
@login_required
def commit_import(job_id):
    job = db.session.get(DataJob, job_id)
    if job is None or job.job_type != "import":
        return err("NOT_FOUND", "任务不存在", status=404)
    project, _ = _project_and_role(job.project_id, "import.run")
    result = excel_service.commit_import(g.user, project, job)
    return ok(result)

@bp.post("/projects/<int:project_id>/testmatrix/import")
@login_required
def import_test_matrix(project_id):
    """Import the fixed Japanese Test-Matrix workbook, mapping its columns onto
    the editor's Test-Matrix based fields (one-step: parse → create/update)."""
    from ...services.lanmatrix import testmatrix_bridge

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

@bp.post("/projects/<int:project_id>/libfunc/import")
@login_required
def import_libfunc(project_id):
    """Import a Lib(Func) workbook: one function block -> one editor row
    (lib_* fields + shared step-detail JSON)."""
    from ...services.lanmatrix import libconst_bridge
    return _import_libconst(project_id, libconst_bridge.import_libfunc)

@bp.post("/projects/<int:project_id>/const/import")
@login_required
def import_const(project_id):
    """Import a Const workbook: one constant definition -> one editor row
    (const_* fields)."""
    from ...services.lanmatrix import libconst_bridge
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

@bp.get("/projects/<int:project_id>/testmatrix/export")
@login_required
def export_test_matrix(project_id):
    """Export the editor's items as a byte-compatible Japanese Test-Matrix
    workbook (summary sheet + per-category detail sheets)."""
    from ...services.lanmatrix import testmatrix_bridge

    project, _ = _project_and_role(project_id, "export.run")
    buf = testmatrix_bridge.export_workbook(project)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_test_matrix_{ts}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@bp.get("/projects/<int:project_id>/libfunc/export")
@login_required
def export_libfunc(project_id):
    """Export the project's Lib(Func) rows as a block-structured .xlsx."""
    from ...services.lanmatrix import libconst_bridge

    project, _ = _project_and_role(project_id, "export.run")
    buf = libconst_bridge.export_libfunc(project)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_libfunc_{ts}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@bp.get("/projects/<int:project_id>/const/export")
@login_required
def export_const(project_id):
    """Export the project's Const rows as a flat-table .xlsx."""
    from ...services.lanmatrix import libconst_bridge

    project, _ = _project_and_role(project_id, "export.run")
    buf = libconst_bridge.export_const(project)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_const_{ts}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@bp.post("/projects/<int:project_id>/exports")
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

@bp.get("/projects/<int:project_id>/audit-logs")
@login_required
def audit_logs(project_id):
    _project_and_role(project_id, "audit.view")
    result = service.list_audit(
        project_id, page=int(request.args.get("page", 1)),
        page_size=int(request.args.get("page_size", settings.PAGE_SIZE)))
    return ok(result)

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
    users = service.search_users(request.args.get("q", ""), limit=20)
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
