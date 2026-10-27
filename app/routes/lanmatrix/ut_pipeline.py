"""Unit-test pipeline endpoints (``/api/v1/projects/<id>/ut/*``).

    GET  /config                         project binding (project.view)
    PUT  /config                         update binding (project.edit)
    POST /targets/import                 re-read the project index (item.edit)
    GET  /targets                        実施一覧 with each target's latest job
    POST /batches                        {target_ids, label} → queue (item.edit)
    GET  /batches                        batches with per-state counts
    GET  /jobs                           ?state&batch_id; host C0/C1/MC/DC digest per job
    GET  /repair-stats                   model-round outcomes per model
    GET  /approved/export                ?batch_id; zip of approved CSVs + manifest
    GET  /jobs/<id>                      job + every attempt (origin, verdict, evidence)
    GET  /jobs/<id>/attempts/<aid>/csv   CP932 download; ?format=text for preview,
                                         ?view=raw for the executed form a revision extends
    GET  /jobs/<id>/attempts/<aid>/coverage  per-line host llvm-cov C0/C1/MC/DC report
    POST /jobs/<id>/revision             human revision: {csv, note} or multipart file
    POST /jobs/<id>/review               {decision: approve|reject, note} (item.review)
    POST /jobs/<id>/retry                blocked / error / stopped → resume (item.edit)
    POST /jobs/<id>/stop                 stop an active job (item.edit)
    POST /batches/<id>/stop              stop every active job of a batch (item.edit)
"""

from __future__ import annotations

from flask import Blueprint, Response, g, request

from ...jobqueue import ut_tasks
from ...services.lanmatrix.errors import ServiceError
from ...services.ut_pipeline import export as ut_export
from ...services.ut_pipeline import pipeline
from ...services.ut_pipeline.repair import normalize_csv
from ._base import _project_and_role, arg_int, arg_str, err, login_required, ok, register_common

bp = Blueprint("lanmatrix_ut", __name__, url_prefix="/api/v1/projects/<int:project_id>/ut")
register_common(bp)

_STATES = ("queued", "generating", "executing", "repairing", "awaiting_review",
           "needs_human", "blocked", "error", "approved", "rejected", "stopped")


def _body() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ServiceError("请求必须是 JSON 对象", code="BAD_REQUEST")
    return body


@bp.get("/config")
@login_required
def get_config(project_id: int):
    _project_and_role(project_id, "project.view")
    config = pipeline.get_config(project_id)
    return ok(pipeline.config_document(config))


@bp.put("/config")
@login_required
def put_config(project_id: int):
    _project_and_role(project_id, "project.edit")
    config = pipeline.save_config(project_id, _body(), g.user.id)
    return ok(pipeline.config_document(config))


@bp.post("/manifest/validate")
@login_required
def validate_manifest(project_id: int):
    _project_and_role(project_id, "project.edit")
    return ok(pipeline.validate_manifest(project_id, _body().get("manifest_document")))


@bp.get("/manifest/options")
@login_required
def manifest_options(project_id: int):
    _project_and_role(project_id, "project.view")
    return ok(pipeline.manifest_options())


@bp.post("/targets/import")
@login_required
def import_targets(project_id: int):
    _project_and_role(project_id, "item.edit")
    return ok(pipeline.import_targets(project_id, g.user.id))


@bp.get("/targets")
@login_required
def list_targets(project_id: int):
    _project_and_role(project_id, "project.view")
    return ok(pipeline.list_targets(project_id))


@bp.post("/batches")
@login_required
def create_batch(project_id: int):
    _project_and_role(project_id, "item.edit")
    body = _body()
    batch = pipeline.create_batch(project_id, body.get("target_ids"), g.user.id,
                                  str(body.get("label") or ""))
    ut_tasks.ut_generate_batch(batch.id)
    return ok(batch.to_dict(), status=202)


@bp.get("/batches")
@login_required
def list_batches(project_id: int):
    _project_and_role(project_id, "project.view")
    return ok(pipeline.list_batches(project_id))


@bp.get("/jobs")
@login_required
def list_jobs(project_id: int):
    _project_and_role(project_id, "project.view")
    return ok(pipeline.list_jobs(
        project_id, state=arg_str("state", allowed=_STATES),
        batch_id=arg_int("batch_id", minimum=1)))


@bp.get("/jobs/<int:job_id>")
@login_required
def job_detail(project_id: int, job_id: int):
    _project_and_role(project_id, "project.view")
    return ok(pipeline.job_detail(project_id, job_id))


@bp.get("/jobs/<int:job_id>/attempts/<int:attempt_id>/csv")
@login_required
def attempt_csv(project_id: int, job_id: int, attempt_id: int):
    _project_and_role(project_id, "project.view")
    if request.args.get("format") == "diagnostic":
        return ok(pipeline.attempt_diagnostic(project_id, job_id, attempt_id))
    data, name = pipeline.attempt_csv(project_id, job_id, attempt_id, raw=request.args.get("view") == "raw")
    if request.args.get("format") == "text":
        return ok({"name": name, "text": data.decode("cp932", "replace")})
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.get("/jobs/<int:job_id>/attempts/<int:attempt_id>/coverage")
@login_required
def attempt_coverage(project_id: int, job_id: int, attempt_id: int):
    _project_and_role(project_id, "project.view")
    return ok(pipeline.attempt_coverage_report(project_id, job_id, attempt_id))


@bp.get("/jobs/<int:job_id>/attempts/<int:attempt_id>/artifacts/<artifact_id>")
@login_required
def attempt_artifact(project_id: int, job_id: int, attempt_id: int, artifact_id: str):
    _project_and_role(project_id, "project.view")
    data, name = pipeline.attempt_artifact(project_id, job_id, attempt_id, artifact_id)
    return Response(data, mimetype="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff"})


@bp.post("/jobs/<int:job_id>/revision")
@login_required
def submit_revision(project_id: int, job_id: int):
    _project_and_role(project_id, "item.edit")
    upload = request.files.get("file")
    if upload is not None:
        data, note = upload.read(), request.form.get("note", "")
    else:
        body = _body()
        text = body.get("csv")
        if not isinstance(text, str) or not text.strip():
            return err("VALIDATION_ERROR", "csv 必须是非空文本", status=400)
        try:
            data = normalize_csv(text)
        except UnicodeEncodeError:
            return err("VALIDATION_ERROR", "CSV 含有 CP932 无法表示的字符", status=400)
        note = str(body.get("note") or "")
    job = pipeline.submit_revision(project_id, job_id, data, g.user.id, note)
    ut_tasks.ut_advance_job(job.id)
    return ok(job.to_dict(), status=202)


@bp.post("/jobs/<int:job_id>/review")
@login_required
def review(project_id: int, job_id: int):
    _project_and_role(project_id, "item.review")
    body = _body()
    expected_attempt_id = body.get("expected_attempt_id")
    if expected_attempt_id is not None and (
            type(expected_attempt_id) is not int or expected_attempt_id < 1):
        return err("VALIDATION_ERROR", "expected_attempt_id 必须是正整数", status=400)
    options = {"expected_attempt_id": expected_attempt_id} if "expected_attempt_id" in body else {}
    job = pipeline.review(project_id, job_id, str(body.get("decision") or ""),
                          g.user.id, str(body.get("note") or ""), **options)
    return ok(job.to_dict())


@bp.post("/jobs/<int:job_id>/retry")
@login_required
def retry(project_id: int, job_id: int):
    _project_and_role(project_id, "item.edit")
    job = pipeline.retry(project_id, job_id, g.user.id)
    ut_tasks.ut_advance_job(job.id)
    return ok(job.to_dict(), status=202)


@bp.post("/jobs/<int:job_id>/stop")
@login_required
def stop_job(project_id: int, job_id: int):
    _project_and_role(project_id, "item.edit")
    job = pipeline.stop_job(project_id, job_id, g.user.id)
    return ok(job.to_dict(), status=202)


@bp.post("/batches/<int:batch_id>/stop")
@login_required
def stop_batch(project_id: int, batch_id: int):
    _project_and_role(project_id, "item.edit")
    return ok(pipeline.stop_batch(project_id, batch_id, g.user.id), status=202)


@bp.get("/repair-stats")
@login_required
def repair_stats(project_id: int):
    _project_and_role(project_id, "project.view")
    return ok(pipeline.repair_stats(project_id))


@bp.get("/approved/export")
@login_required
def export_approved(project_id: int):
    _project_and_role(project_id, "project.view")
    data, name = ut_export.build_archive(project_id, g.user.id, arg_int("batch_id", None))
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
