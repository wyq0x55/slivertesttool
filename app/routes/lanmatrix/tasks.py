"""Project upload-task (test execution) endpoints for the LAN Test Matrix API."""

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
    dbadmin, excel_service, permissions, service, settings,
)
from ...services.lanmatrix.service import ServiceError
from ._base import (
    ok, err, current_user, login_required, system_admin_required,
    register_common, _project_and_role, _client_ip,
    _LOCK_THRESHOLD, _LOCK_MINUTES,
)

bp = Blueprint("lanmatrix_tasks", __name__, url_prefix="/api/v1")
register_common(bp)

def _enqueue_task(task: Task) -> None:
    from ...jobqueue.tasks import run_task
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

@bp.get("/projects/<int:project_id>/tasks")
@login_required
def list_project_tasks(project_id):
    _, role = _project_and_role(project_id, "task.view")
    tasks = task_service.list_tasks(project_id=project_id, limit=1000)
    status = license_service.get_status()
    status["queued_jobs"] = Task.query.filter_by(
        project_id=project_id, status=TaskStatus.QUEUED.value).count()
    return ok({"tasks": [t.to_dict() for t in tasks],
               "models": project_model_service.effective_models(project_id),
               "license": status,
               "role": role,
               "can_delete": permissions.can("task.delete", role,
                                              is_system_admin=g.user.is_system_admin)})

@bp.post("/projects/<int:project_id>/tasks/upload-tree")
@login_required
def upload_project_tree(project_id):
    """Upload a test-case folder tree and queue the chosen test ids for a
    project. Only project members reach this (``task.upload``)."""
    from pathlib import Path

    import shutil

    from ...runners import run_layout

    project, _ = _project_and_role(project_id, "task.upload")
    cfg = current_app.config_obj
    if not project_model_service.effective_has(project_id):
        return err("NO_MODEL", "该项目尚未添加 .sil 模型，请先在“模型管理”中添加", status=409)

    model_name = (request.form.get("model") or "").strip()
    if not model_name:
        default = project_model_service.effective_default(project_id)
        model_name = default["name"] if default else ""
    model_path = project_model_service.effective_path(project_id, model_name)
    if model_path is None:
        return err("BAD_MODEL", "未知模型，请选择该项目已添加的 .sil 模型", status=400)

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

@bp.post("/projects/<int:project_id>/tasks/run-selected")
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

    from ...models import TestItemRow
    from ...runners import run_layout
    from ...services.lanmatrix import silver_json_export as sje

    project, _ = _project_and_role(project_id, "task.upload")
    cfg = current_app.config_obj
    if not project_model_service.effective_has(project_id):
        return err("NO_MODEL", "该项目尚未添加 .sil 模型，请先在“模型管理”中添加", status=409)

    body = request.get_json(silent=True) or {}
    raw_ids = body.get("test_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return err("VALIDATION_ERROR", "请至少选择一个 test id 提交", status=400)
    selected = [str(t).strip() for t in raw_ids if str(t).strip()]

    model_name = (body.get("model") or "").strip()
    if not model_name:
        default = project_model_service.effective_default(project_id)
        model_name = default["name"] if default else ""
    model_path = project_model_service.effective_path(project_id, model_name)
    if model_path is None:
        return err("BAD_MODEL", "未知模型，请选择该项目已添加的 .sil 模型", status=400)
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

@bp.get("/projects/<int:project_id>/tasks/<task_key>")
@login_required
def project_task_status(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.view")
    return ok({"task": task.to_dict()})

@bp.get("/projects/<int:project_id>/tasks/<task_key>/detail")
@login_required
def project_task_detail(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.view")
    events = event_service.fetch_since(task.id, 0, limit=5000)
    data = task.to_dict(detail=True)
    data["events"] = [e.to_dict() for e in events]
    return ok({"task": data})

@bp.get("/projects/<int:project_id>/tasks/<task_key>/stream")
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

@bp.get("/projects/<int:project_id>/tasks/<task_key>/jdgrslt")
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
    from ...runners.test_runner import (
        count_failed_steps, extract_case_section, parse_verdict_text,
    )
    content = extract_case_section(raw, task.test_id)
    verdict = task.result
    if task.status in (TaskStatus.PASSED.value, TaskStatus.FAILED.value):
        verdict = parse_verdict_text(raw, task.test_id)
    return ok({"available": True, "verdict": verdict,
               "failed_steps": count_failed_steps(raw, task.test_id),
               "content": content})

@bp.post("/projects/<int:project_id>/tasks/<task_key>/cancel")
@login_required
def cancel_project_task(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.cancel")
    result = task_service.request_cancel(task)
    return ok({"task_id": task.task_key, "result": result, "message": task.message})

@bp.delete("/projects/<int:project_id>/tasks/<task_key>")
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

    from ...runners import run_layout
    shutil.rmtree(run_layout.log_dir(workspace, test_id), ignore_errors=True)
    shutil.rmtree(run_layout.staging_dir(workspace, test_id), ignore_errors=True)

@bp.get("/projects/<int:project_id>/tasks/<task_key>/download")
@login_required
def download_project_task(project_id, task_key):
    _, task = _require_task(project_id, task_key, "task.download")
    buffer = report_service.build_report_stream(task)
    if buffer is None:
        return err("NOT_FOUND", "该任务暂无可下载的报告", status=404)
    from ...runners import run_layout
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

@bp.post("/projects/<int:project_id>/tasks/delete_batch")
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

@bp.get("/projects/<int:project_id>/tasks/download_batch")
@login_required
def download_project_tasks_batch(project_id):
    """Bundle the reports of several of a project's tasks into one zip.

    Query: ``?keys=T000001,T000002`` (or repeated ``keys`` params). Requires
    ``task.download``; keys are resolved within the project.
    """
    project, _ = _project_and_role(project_id, "task.download")
    from ...runners import run_layout
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
