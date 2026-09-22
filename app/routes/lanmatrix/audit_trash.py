"""LAN Matrix route ownership: audit_trash."""

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


