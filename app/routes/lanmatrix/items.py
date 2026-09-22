"""LAN Matrix route ownership: items."""

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
from .projects import _collab_write_blocked


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

