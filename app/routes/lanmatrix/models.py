"""LAN Matrix route ownership: models."""

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

