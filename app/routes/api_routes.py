"""REST + SSE API.

Endpoints (all JSON unless noted):

    POST   /api/tasks/upload_tree      upload a test-case folder, queue selected
    GET    /api/tasks                  list tasks
    GET    /api/tasks/<key>            task status
    GET    /api/tasks/<key>/detail     task detail + recorded events
    GET    /api/tasks/<key>/stream     Server-Sent Events (live log/progress)
    POST   /api/tasks/<key>/cancel     cancel a task
    POST   /api/tasks/cancel_batch     cancel several tasks at once
    GET    /api/tasks/<key>/jdgrslt     judge result log (jdgrslt.log) as text
    GET    /api/tasks/<key>/download   download the result report (zip)
    GET    /api/tasks/download_batch   download several reports as one zip
    GET    /api/licenses               license/concurrency status
    GET    /api/models                 registered .sil models (names, for pickers)
    POST   /api/admin/verify           check the current system-admin session
    GET    /api/admin/models           registered .sil models incl. paths (admin)
    POST   /api/admin/models           register a server-side .sil path (admin)
    POST   /api/admin/models/bulk      replace the whole model list (admin)
    DELETE /api/admin/models           remove a registered model (admin)
    POST   /api/admin/license          change the license limit (admin)
    POST   /api/admin/tasks/<key>/cancel  cancel any task (admin)
    POST   /api/admin/tasks/cancel_batch  cancel several tasks (admin)
    DELETE /api/admin/tasks/<key>      delete a task (admin)
    POST   /api/admin/tasks/delete_batch  delete several tasks (admin)
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    request,
    send_file,
    session,
    stream_with_context,
)

from ..extensions import db
from ..models import Task, TaskStatus
from ..services import (
    event_service,
    license_service,
    model_service,
    report_service,
    task_service,
    upload_service,
)
from ..services.upload_service import UploadError

api_bp = Blueprint("api", __name__, url_prefix="/api")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cfg():
    return current_app.config_obj


def _enqueue(task: Task) -> None:
    # Imported lazily to avoid an import cycle (tasks -> create_app -> routes).
    from ..jobqueue.tasks import run_task

    run_task(task.id)


def _require_admin() -> bool:
    """Authorise admin actions via the unified system-admin session (RBAC).

    The former ``ADMIN_TOKEN`` header/field path has been removed. Authority now
    comes solely from the LAN Test Matrix session: only a logged-in account with
    ``is_system_admin`` is allowed. This closes the privilege-escalation hole
    where the shared (default) ``ADMIN_TOKEN`` let *any* logged-in user perform
    admin actions, and matches the session RBAC enforced by ``/api/v1/admin/*``.
    """
    uid = session.get("lm_user_id")
    if uid is None:
        return False
    from ..services.lanmatrix import service

    user = service.get_user(uid)
    return bool(user and user.is_active and user.is_system_admin)


def _task_or_404(task_key: str) -> Task | None:
    return task_service.get_by_key(task_key)


def _items(files_field: str, paths_field: str):
    """Zip an uploaded (files, paths) pair into ``(path, FileStorage)`` items."""
    files = request.files.getlist(files_field)
    if not files:
        return []
    paths = request.form.getlist(paths_field)
    if len(paths) == len(files):
        return list(zip(paths, files))
    return [(f.filename, f) for f in files]


# --------------------------------------------------------------------------- #
# Folder upload (primary path): upload a test-case tree, queue selected ids
# --------------------------------------------------------------------------- #
@api_bp.post("/tasks/upload_tree")
def upload_tree():
    """Upload a selected test-case folder and queue the chosen test ids.

    Form fields:
      * ``files`` / ``paths``            -- the test-case folder tree.
      * ``lib_files`` / ``lib_paths``    -- contents of the tester's ``lib``
        folder (optional); inlined into each judge.
      * ``stdlib_files`` / ``stdlib_paths`` -- contents of the ``stdlib`` folder
        (optional); inlined into each judge.
      * ``test_ids``                     -- the chosen test ids.
      * ``model``                        -- name of the registered ``.sil`` model
        to run against.
      * ``submitter`` / ``folder_name``  -- metadata.
    """
    cfg = _cfg()
    if not model_service.has_models():
        return jsonify(
            error="No .sil model has been registered. Ask an administrator to "
                  "add one on the Admin page before submitting tests."
        ), 409

    model_name = (request.form.get("model") or "").strip()
    if not model_name:
        default = model_service.default_model()
        model_name = default["name"] if default else ""
    model_path = model_service.get_model_path(model_name)
    if model_path is None:
        return jsonify(
            error="Unknown model. Pick one of the registered .sil models.",
            models=[m["name"] for m in model_service.list_models()],
        ), 400

    if not request.files.getlist("files"):
        return jsonify(error="No files received. Select a test-case folder."), 400

    try:
        info = upload_service.stage_tree(
            _items("files", "paths"),
            cfg.WORKSPACE_DIR,
            lib_items=_items("lib_files", "lib_paths"),
            stdlib_items=_items("stdlib_files", "stdlib_paths"),
        )
    except UploadError as exc:
        return jsonify(error=str(exc)), 400

    valid = set(info["test_ids"])
    selected = [t for t in request.form.getlist("test_ids") if t in valid]
    if not selected:
        upload_service.cleanup_staging(cfg.WORKSPACE_DIR, info["upload_key"])
        return jsonify(
            error="Select at least one test id to submit.",
            test_ids=info["test_ids"],
        ), 400

    submitter = (request.form.get("submitter") or "anonymous").strip() or "anonymous"
    folder_name = (request.form.get("folder_name") or "").strip() or "(folder upload)"
    sil_ref = str(Path(model_path).resolve())

    created, duplicates, errors = [], [], []
    try:
        for test_id in selected:
            existing = task_service.find_active_duplicate(submitter, test_id)
            if existing is not None:
                duplicates.append({"test_id": test_id, "task_id": existing.task_key})
                continue
            try:
                task = task_service.create_task(
                    task_name=test_id,
                    file_name=folder_name,
                    submitter=submitter,
                    test_id=test_id,
                    sil_relpath=sil_ref,
                    sil_name=model_name,
                    workspace="",
                )
                import shutil

                from ..runners import run_layout
                proj_root = Path(cfg.WORKSPACE_DIR) / "_legacy"
                case_dir = run_layout.staging_dir(proj_root, test_id) / test_id
                shutil.rmtree(case_dir.parent, ignore_errors=True)
                upload_service.materialise_one(
                    cfg.WORKSPACE_DIR, info["upload_key"], case_dir, test_id,
                )
                task.workspace = str(proj_root)
                db.session.commit()
                _enqueue(task)
                created.append({"test_id": test_id, "task_id": task.task_key})
            except UploadError as exc:
                errors.append({"test_id": test_id, "error": str(exc)})
    finally:
        upload_service.cleanup_staging(cfg.WORKSPACE_DIR, info["upload_key"])

    return jsonify(
        created=created,
        duplicates=duplicates,
        errors=errors,
        notes=info.get("notes", []),
    ), 201


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
@api_bp.get("/tasks")
def list_tasks():
    submitter = request.args.get("submitter")
    tasks = task_service.list_tasks(submitter=submitter, limit=1000)
    return jsonify(tasks=[t.to_dict() for t in tasks])


@api_bp.get("/tasks/<task_key>")
def task_status(task_key: str):
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    return jsonify(task.to_dict())


@api_bp.get("/tasks/<task_key>/detail")
def task_detail(task_key: str):
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    events = event_service.fetch_since(task.id, 0, limit=5000)
    data = task.to_dict(detail=True)
    data["events"] = [e.to_dict() for e in events]
    return jsonify(data)


# --------------------------------------------------------------------------- #
# SSE stream
# --------------------------------------------------------------------------- #
@api_bp.get("/tasks/<task_key>/stream")
def task_stream(task_key: str):
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    task_pk = task.id
    cfg = _cfg()
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
            # Stop once the task is final and no more events remain.
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


# --------------------------------------------------------------------------- #
# Cancel / download
# --------------------------------------------------------------------------- #
@api_bp.post("/tasks/<task_key>/cancel")
def cancel_task(task_key: str):
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    result = task_service.request_cancel(task)
    return jsonify(task_id=task.task_key, result=result, message=task.message)


@api_bp.post("/tasks/cancel_batch")
def cancel_batch():
    """Cancel several tasks at once (leaves the execution queue).

    Body: ``{"keys": ["T000001", ...]}``.
    """
    data = request.get_json(silent=True) or request.form
    keys = data.get("keys") or request.form.getlist("keys")
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    results = []
    for key in keys or []:
        task = _task_or_404(key)
        if task is None:
            results.append({"task_id": key, "result": "not_found"})
            continue
        results.append({"task_id": key, "result": task_service.request_cancel(task)})
    return jsonify(results=results)


@api_bp.get("/tasks/<task_key>/jdgrslt")
def judge_result_log(task_key: str):
    """Return the judge result log (``jdgrslt.log``) as JSON text lines.

    The client renders it and highlights failing steps. ``verdict`` echoes the
    parsed task verdict for convenience.
    """
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    path = report_service.jdgrslt_path(task)
    if path is None:
        return jsonify(available=False, verdict=task.result,
                       content="", message="No jdgrslt.log for this task yet."), 200
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return jsonify(available=False, verdict=task.result,
                       content="", message=f"Could not read jdgrslt.log: {exc}"), 200

    # Scope to this task's own test-case section (a jdgrslt.log may contain
    # several cases) so the viewer and the verdict never inherit another case's
    # result.
    from ..runners.test_runner import (
        count_failed_steps,
        extract_case_section,
        parse_verdict_text,
    )

    content = extract_case_section(raw, task.test_id)
    verdict = task.result
    if task.status in (TaskStatus.PASSED.value, TaskStatus.FAILED.value):
        verdict = parse_verdict_text(raw, task.test_id)
        # Self-heal a stale verdict recorded by an older parser (e.g. one that
        # read the whole multi-case file), keeping list + panel consistent.
        new_status = (
            TaskStatus.PASSED.value if verdict.upper().startswith("PASS")
            else TaskStatus.FAILED.value
        )
        if verdict != task.result or new_status != task.status:
            task.result = verdict
            task.status = new_status
            task.message = f"Execution finished. Verdict: {verdict}."
            db.session.add(task)
            db.session.commit()
    return jsonify(available=True, verdict=verdict,
                   failed_steps=count_failed_steps(raw, task.test_id),
                   content=content)


@api_bp.get("/tasks/<task_key>/download")
def download_report(task_key: str):
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    buffer = report_service.build_report_stream(task)
    if buffer is None:
        return jsonify(error="No report available for this task."), 404
    from ..runners import run_layout
    # ``test_id`` is unique within a project, so it alone identifies a run's
    # results; no ``<task_key>_`` prefix is needed to disambiguate downloads.
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{run_layout.safe_tid(task.test_id)}_report.zip",
    )


@api_bp.get("/tasks/download_batch")
def download_batch():
    """Bundle the reports of several tasks into one flat downloadable zip.

    Each task's report contents are unpacked into their own folder inside the
    bundle (``<test_id>/...``) instead of nesting one zip per testcase, so the
    user does not have to unzip every testcase individually.

    Query: ``?keys=T000001,T000002`` (or repeated ``keys`` params).
    """
    from ..runners import run_layout
    keys = request.args.getlist("keys")
    if len(keys) == 1 and "," in keys[0]:
        keys = [k.strip() for k in keys[0].split(",") if k.strip()]
    added = 0
    buffer = io.BytesIO()
    seen_folders: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for key in keys:
            task = _task_or_404(key)
            if task is None:
                continue
            # Compress each task's results on demand and place them under a
            # dedicated folder, so the bundle holds the individual result files
            # directly instead of nesting one zip per testcase. ``test_id`` is
            # unique per project, so it alone names the folder.
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
        return jsonify(error="None of the selected tasks have a report."), 404
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"reports_{added}_tasks.zip",
    )


# --------------------------------------------------------------------------- #
# Licenses + models
# --------------------------------------------------------------------------- #
@api_bp.get("/licenses")
def licenses():
    status = license_service.get_status()
    status["queued_jobs"] = Task.query.filter_by(
        status=TaskStatus.QUEUED.value
    ).count()
    return jsonify(status)


@api_bp.get("/models")
def models():
    """Public: registered model names (for the submit-page picker)."""
    return jsonify(models=model_service.list_models())


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@api_bp.post("/admin/verify")
def admin_verify():
    """Report whether the current session is an authorised system admin.

    Authority is the unified system-admin session (no ``ADMIN_TOKEN``): the UI
    unlocks admin controls only when the logged-in account is a system
    administrator.
    """
    if not _require_admin():
        return jsonify(ok=False, enabled=True,
                       error="System administrator session required."), 401
    return jsonify(ok=True, enabled=True)


@api_bp.get("/admin/models")
def admin_list_models():
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    return jsonify(models=model_service.list_models(include_path=True))


@api_bp.post("/admin/models")
def admin_add_model():
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    data = request.get_json(silent=True) or request.form
    try:
        entry = model_service.add_model(data.get("name", ""), data.get("path", ""))
    except model_service.ModelError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, model=entry,
                   models=model_service.list_models(include_path=True))


@api_bp.post("/admin/models/bulk")
def admin_bulk_models():
    """Replace the whole model list. Body: ``{"models": [{name?, path}, ...]}``."""
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    data = request.get_json(silent=True) or {}
    entries = data.get("models") or []
    try:
        result = model_service.replace_models(entries)
    except model_service.ModelError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, models=result)


@api_bp.delete("/admin/models")
def admin_remove_model():
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    removed = model_service.remove_model(name)
    return jsonify(ok=removed, models=model_service.list_models(include_path=True))


@api_bp.post("/admin/license")
def admin_set_license():
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    data = request.get_json(silent=True) or request.form
    try:
        count = int(data.get("count"))
    except (TypeError, ValueError):
        return jsonify(error="'count' must be an integer >= 1."), 400
    try:
        applied = license_service.set_limit(count)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, count=applied)


@api_bp.post("/admin/tasks/<task_key>/cancel")
def admin_cancel_task(task_key: str):
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    result = task_service.request_cancel(task)
    return jsonify(task_id=task.task_key, result=result)


def _delete_task_and_workspace(task: Task) -> None:
    """Cancel a task if still active, then remove its record + result dirs.

    ``task.workspace`` is a shared per-project root, so only this test id's
    subtree is removed.
    """
    if not TaskStatus(task.status).is_final:
        task_service.request_cancel(task)
    workspace, test_id = task.workspace, task.test_id
    task_service.delete_task(task)
    if workspace and test_id:
        import shutil

        from ..runners import run_layout
        shutil.rmtree(run_layout.log_dir(workspace, test_id), ignore_errors=True)
        shutil.rmtree(run_layout.staging_dir(workspace, test_id), ignore_errors=True)


@api_bp.delete("/admin/tasks/<task_key>")
def admin_delete_task(task_key: str):
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    task = _task_or_404(task_key)
    if task is None:
        return jsonify(error="Task not found."), 404
    _delete_task_and_workspace(task)
    return jsonify(ok=True)


@api_bp.post("/admin/tasks/cancel_batch")
def admin_cancel_batch():
    """Cancel several tasks at once (admin). Body: ``{"keys": [...]}``."""
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    data = request.get_json(silent=True) or request.form
    keys = data.get("keys") or request.form.getlist("keys")
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    results = []
    for key in keys or []:
        task = _task_or_404(key)
        if task is None:
            results.append({"task_id": key, "result": "not_found"})
            continue
        results.append({"task_id": key, "result": task_service.request_cancel(task)})
    return jsonify(results=results)


@api_bp.post("/admin/tasks/delete_batch")
def admin_delete_batch():
    """Delete several tasks + their workspaces at once (admin).

    Body: ``{"keys": ["T000001", ...]}``.
    """
    if not _require_admin():
        return jsonify(error="Admin authorisation required."), 401
    data = request.get_json(silent=True) or request.form
    keys = data.get("keys") or request.form.getlist("keys")
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    results = []
    for key in keys or []:
        task = _task_or_404(key)
        if task is None:
            results.append({"task_id": key, "result": "not_found"})
            continue
        _delete_task_and_workspace(task)
        results.append({"task_id": key, "result": "deleted"})
    return jsonify(results=results)
