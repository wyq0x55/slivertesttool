"""LAN Matrix route ownership: imports_exports."""

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

