"""LAN Matrix route ownership: fields."""

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
