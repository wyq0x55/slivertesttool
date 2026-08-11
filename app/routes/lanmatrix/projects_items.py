"""Project / field / item CRUD, batch operations, comments, Excel import-export, audit logs and membership for the LAN Test Matrix API."""

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
            created_by=g.user.id,
            version=body.get("version"), version_note=body.get("version_note"))
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
            current_app.config_obj, pdb=pdb, created_by=g.user.id,
            version=request.form.get("version"),
            version_note=request.form.get("version_note"))
    except project_model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"model": entry,
               "models": project_model_service.list_models(
                   project_id, include_path=True)}, status=201)

@bp.post("/projects/<int:project_id>/models/current")
@login_required
def set_current_project_model(project_id):
    _project_and_role(project_id, "model.manage")
    body = request.get_json(silent=True) or {}
    try:
        models = project_model_service.set_current(
            project_id, (body.get("name") or "").strip())
    except project_model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"models": models})

@bp.patch("/projects/<int:project_id>/models/version")
@login_required
def update_project_model_version(project_id):
    """Relabel a registered model (version + release note).

    Separate from model creation on purpose: a version label is usually decided
    *after* the model has been uploaded and smoke-tested, and re-labelling
    changes how future test evidence is grouped, so it is an audited operation
    of its own rather than a silent field edit.
    """
    _project_and_role(project_id, "model.manage")
    body = request.get_json(silent=True) or {}
    try:
        entry = project_model_service.update_version(
            project_id, (body.get("name") or "").strip(),
            body.get("version"), body.get("version_note"),
            updated_by=g.user.id)
    except project_model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"model": entry,
               "models": project_model_service.list_models(
                   project_id, include_path=True)})

@bp.post("/projects/<int:project_id>/models/deprecate")
@login_required
def deprecate_project_model(project_id):
    """Hide a superseded model from the pickers without deleting its history."""
    _project_and_role(project_id, "model.manage")
    body = request.get_json(silent=True) or {}
    try:
        entry = project_model_service.set_deprecated(
            project_id, (body.get("name") or "").strip(),
            bool(body.get("deprecated", True)))
    except project_model_service.ModelError as exc:
        return err("VALIDATION_ERROR", str(exc), status=400)
    return ok({"model": entry,
               "models": project_model_service.list_models(
                   project_id, include_path=True)})

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

# --------------------------------------------------------------------------- #
# In-app SBS editor (bundle models): read / save (optimistic lock) + history
# --------------------------------------------------------------------------- #
@bp.get("/projects/<int:project_id>/models/sbs")
@login_required
def get_model_sbs(project_id):
    _project_and_role(project_id, "model.manage")
    name = (request.args.get("name") or "").strip()
    return ok({"sbs": sbs_service.read_sbs(project_id, name)})

@bp.put("/projects/<int:project_id>/models/sbs")
@login_required
def save_model_sbs(project_id):
    _project_and_role(project_id, "model.manage")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    try:
        result = sbs_service.write_sbs(
            project_id, name, body.get("content"),
            (body.get("base_version") or "").strip(),
            author_id=g.user.id, client_ip=_client_ip())
    except sbs_service.SbsConflict as exc:
        return err(exc.code, str(exc), details=exc.server_data, status=409)
    return ok({"sbs": result})

@bp.get("/projects/<int:project_id>/models/sbs/revisions")
@login_required
def list_model_sbs_revisions(project_id):
    _project_and_role(project_id, "model.manage")
    name = (request.args.get("name") or "").strip()
    return ok({"revisions": sbs_service.list_revisions(project_id, name)})

@bp.get("/projects/<int:project_id>/models/sbs/revisions/<int:revision_id>")
@login_required
def get_model_sbs_revision(project_id, revision_id):
    _project_and_role(project_id, "model.manage")
    name = (request.args.get("name") or "").strip()
    return ok({"revision": sbs_service.get_revision(project_id, name, revision_id)})

@bp.post("/projects/<int:project_id>/models/sbs/revisions/<int:revision_id>/restore")
@login_required
def restore_model_sbs_revision(project_id, revision_id):
    _project_and_role(project_id, "model.manage")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    try:
        result = sbs_service.restore_revision(
            project_id, name, revision_id,
            author_id=g.user.id, client_ip=_client_ip())
    except sbs_service.SbsConflict as exc:
        return err(exc.code, str(exc), details=exc.server_data, status=409)
    return ok({"sbs": result})

@bp.get("/projects/<int:project_id>/items")
@login_required
def list_items(project_id):
    _project_and_role(project_id, "item.view")
    parsed_filters = arg_json("filter", [])
    result = service.list_items(
        project_id,
        page=arg_int("page", 1, minimum=1),
        page_size=arg_int("page_size", settings.PAGE_SIZE,
                          minimum=1, maximum=settings.PAGE_SIZE_MAX),
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

@bp.post("/projects/<int:project_id>/pool/<sheet>/entries")
@login_required
def add_pool_entry(project_id, sheet):
    """Add one reference-pool row (``io`` / ``const``) from the step editor.

    REST path only: when collaboration is live the client inserts through the
    shared Y.Doc instead (this endpoint is then collab-blocked). Provisions the
    pool's field set on demand and enforces its uniqueness contract server-side.
    """
    project, _ = _project_and_role(project_id, "item.create")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    blocked = _collab_write_blocked(project_id)
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    values = body.get("values", {})
    if not isinstance(values, dict):
        values = {}
    item = service.add_pool_entry(g.user, project, sheet, values)
    return ok({"item": item.to_dict()}, status=201)

@bp.post("/projects/<int:project_id>/pool/<sheet>/fields")
@login_required
def ensure_pool_fields(project_id, sheet):
    """Provision a reference pool's field set (``io`` / ``const``) so its columns
    render. Idempotent; used by the collaboration path, where rows are inserted
    through the Y.Doc but the field definitions still live in the DB."""
    project, _ = _project_and_role(project_id, "item.create")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    from ...services.lanmatrix import fields as fld, fields_service
    specs = {"io": fld.IO_FIELDS, "const": fld.CONST_FIELDS}.get(sheet)
    if specs is None:
        return err("VALIDATION_ERROR", "不支持的参考池", status=400)
    created = fields_service.ensure_fields(g.user, project, specs)
    return ok({"created": created})

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

@bp.post("/projects/<int:project_id>/io/import")
@login_required
def import_io(project_id):
    """Import an 入出力 (I/O signal pool) workbook: one signal -> one editor row
    (io_name / io_path / io_note), keeping name AND path unique."""
    from ...services.lanmatrix import libconst_bridge
    return _import_libconst(project_id, libconst_bridge.import_io)

@bp.post("/projects/<int:project_id>/io/extract")
@login_required
def extract_io(project_id):
    """Harvest I/O signal declarations from step procedures into the 入出力 pool.

    Scans the ``input_signals`` / ``expected_signals`` of the requested sheets'
    step docs (``lib`` by default; ``test`` optionally), de-duplicates on
    name+path, and merges the result into the pool with the same uniqueness and
    per-row error reporting as an Excel import."""
    from ...services.lanmatrix import libconst_bridge

    project, _ = _project_and_role(project_id, "import.run")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode") or request.form.get("mode") or "upsert"
    if mode == "replace_all":
        _project_and_role(project_id, "import.replace")
    raw_sheets = payload.get("sheets")
    if raw_sheets is None:
        form_sheets = request.form.get("sheets")
        raw_sheets = form_sheets.split(",") if form_sheets else ["lib"]
    sheets = [str(s).strip() for s in raw_sheets if str(s).strip()] or ["lib"]
    try:
        summary = libconst_bridge.extract_io_from_steps(
            g.user, project, sheets=sheets, mode=mode, source_label="extract")
    except (ServiceError, PermissionDenied, VersionConflict):
        raise
    except Exception as exc:  # noqa: BLE001 - never leak an opaque HTML 500
        current_app.logger.exception("IO extract crashed")
        return err("IMPORT_PARSE_ERROR",
                   f"抽取失败：{type(exc).__name__}: {exc}", status=400)
    return ok({"summary": summary}, status=201)

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

@bp.get("/projects/<int:project_id>/io/export")
@login_required
def export_io(project_id):
    """Export the project's 入出力 pool rows as a flat-table .xlsx."""
    from ...services.lanmatrix import libconst_bridge

    project, _ = _project_and_role(project_id, "export.run")
    buf = libconst_bridge.export_io(project)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, as_attachment=True,
                     download_name=f"{project.code}_io_{ts}.xlsx",
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
    date_from = arg_date("date_from")
    date_to = arg_date("date_to", end_of_day=True)
    if date_from and date_to and date_from > date_to:
        return err("VALIDATION_ERROR", "date_from 不能晚于 date_to", status=400)
    result = service.list_audit(
        project_id, page=arg_int("page", 1, minimum=1),
        page_size=arg_int("page_size", settings.PAGE_SIZE,
                          minimum=1, maximum=settings.PAGE_SIZE_MAX),
        actor_id=arg_int("actor_id", None, minimum=1),
        action=arg_str("action", max_length=48),
        object_type=arg_str("object_type", max_length=32),
        result=arg_str("result", allowed=service.AUDIT_RESULTS),
        date_from=date_from, date_to=date_to,
        q=arg_str("q", max_length=128))
    return ok(result)


@bp.get("/projects/<int:project_id>/audit-logs.csv")
@login_required
def audit_logs_csv(project_id):
    """Download the *filtered* audit log as CSV.

    Deliberately server-side rather than exporting the rows the browser happens
    to have loaded: the table pages 50 at a time, so a client-side export would
    hand the reviewer a file that looks complete and covers the first page only.
    """
    project, _role = _project_and_role(project_id, "audit.view")
    date_from = arg_date("date_from")
    date_to = arg_date("date_to", end_of_day=True)
    if date_from and date_to and date_from > date_to:
        return err("VALIDATION_ERROR", "date_from 不能晚于 date_to", status=400)
    rows = service.audit_csv_rows(
        project_id,
        actor_id=arg_int("actor_id", None, minimum=1),
        action=arg_str("action", max_length=48),
        object_type=arg_str("object_type", max_length=32),
        result=arg_str("result", allowed=service.AUDIT_RESULTS),
        date_from=date_from, date_to=date_to,
        q=arg_str("q", max_length=128))

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        # UTF-8 BOM: without it Excel on a Chinese Windows install reads the
        # file as GBK and every label in this export turns to mojibake.
        yield "\ufeff"
        for row in rows:
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    audit.record("audit.export", actor_id=current_user().id,
                 object_type="project", object_id=project.id,
                 project_id=project.id, client_ip=_client_ip(),
                 new_value={"format": "csv", "filters": dict(request.args)},
                 commit=True)
    return Response(
        stream_with_context(generate()),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{project.code}_audit_{ts}.csv"',
                 "Cache-Control": "no-store"})


@bp.get("/projects/<int:project_id>/audit-logs/actions")
@login_required
def audit_log_actions(project_id):
    """Distinct actions and actors, so the filter dropdowns reflect real data."""
    _project_and_role(project_id, "audit.view")
    return ok({"actions": service.audit_actions(project_id),
               "actors": service.audit_actors(project_id)})


# --------------------------------------------------------------------------- #
# Recycle bin
# --------------------------------------------------------------------------- #
@bp.get("/projects/<int:project_id>/trash")
@login_required
def list_trash(project_id):
    """Everything the project has deleted, with its remaining retention."""
    _project_and_role(project_id, "trash.view")
    kind = arg_str("kind", allowed=trash_service.KINDS)
    result = trash_service.list_trash(
        project_id, kind=kind or None,
        limit=trash_service.clamp_limit(request.args.get("limit")))
    return ok(result)


def _trash_target():
    """Read and validate ``kind`` + ``id`` from a restore/purge body."""
    body = request.get_json(silent=True) or request.form or {}
    kind = (body.get("kind") or "").strip()
    obj_id = body.get("id")
    if kind not in trash_service.KINDS:
        raise ServiceError("未知的回收站类型: %s" % (kind or "(空)"),
                           code="VALIDATION_ERROR")
    if obj_id in (None, ""):
        raise ServiceError("缺少参数 id", code="VALIDATION_ERROR")
    return kind, obj_id


@bp.post("/projects/<int:project_id>/trash/restore")
@login_required
def restore_from_trash(project_id):
    project, _role = _project_and_role(project_id, "trash.restore")
    kind, obj_id = _trash_target()
    entry = trash_service.restore(current_user(), project, kind, obj_id)
    return ok({"restored": entry,
               "remaining": trash_service.count_trash(project_id)})


@bp.post("/projects/<int:project_id>/trash/purge")
@login_required
def purge_from_trash(project_id):
    """Delete one entry for real, ahead of its retention date."""
    project, _role = _project_and_role(project_id, "trash.purge")
    kind, obj_id = _trash_target()
    entry = trash_service.purge(current_user(), project, kind, obj_id)
    return ok({"purged": entry,
               "remaining": trash_service.count_trash(project_id)})


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


@bp.get("/projects/<int:project_id>/categories")
@login_required
def list_project_categories(project_id: int):
    """Distinct テスト区分 in this project, with names and case counts.

    Exists so the routing editor can offer the 区分 that actually exist instead
    of making an admin type them from memory: a mistyped 区分 produces a rule
    that never matches, and nothing in the UI would ever say so.
    """
    from ...models import TestItemRow
    from ...services.lanmatrix import review_routes as rr

    _project_and_role(project_id, "project.view")
    rows = (db.session.query(TestItemRow.custom_values)
            .filter(TestItemRow.project_id == project_id,
                    TestItemRow.deleted_at.is_(None),
                    TestItemRow.sheet == "test")
            .all())

    tally: dict[str, dict] = {}
    for (values,) in rows:
        values = values or {}
        key = rr.normalise_category(values.get(rr.CATEGORY_KEY))
        if not key:
            continue
        entry = tally.setdefault(
            key, {"category": key, "category_name": "", "count": 0})
        entry["count"] += 1
        if not entry["category_name"]:
            entry["category_name"] = str(
                values.get(rr.CATEGORY_NAME_KEY) or "").strip()

    # Numeric 区分 first and in numeric order (1, 2, 10 -- not 1, 10, 2), which
    # is the order the editor's category pager already uses.
    def sort_key(item: dict):
        raw = item["category"]
        try:
            return (0, float(raw), "")
        except ValueError:
            return (1, 0.0, raw)

    return ok({"categories": sorted(tally.values(), key=sort_key)})


@bp.get("/projects/<int:project_id>/dashboard")
@login_required
def project_dashboard_data(project_id: int):
    """Progress, trend, per-version and review aggregates for one project.

    Served as a single bundle rather than four endpoints so the page cannot
    render a progress ring and a review funnel computed seconds apart.

    Gated on ``project.view``: this is a read-only summary of data the member
    can already see row by row, so requiring a stronger capability would only
    push people back to counting cells by hand.
    """
    from ...services.lanmatrix import dashboard_service

    project, role = _project_and_role(project_id, "project.view")
    data = dashboard_service.snapshot(project)
    # The review-policy panel reuses this payload. Tell it up front whether this
    # user may change the policy: without the flag a reader is shown live
    # checkboxes that only fail on save, which reads as a broken page rather
    # than as a permission boundary.
    data["can_edit_policy"] = permissions.can(
        "project.edit", role, is_system_admin=g.user.is_system_admin)
    return ok(data)
