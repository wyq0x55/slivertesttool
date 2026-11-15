"""Variable curves: a task attempt's output.csv with its verdict and signals.

Every read binds to the task (and attempt) named in the path and checks the
caller's project role. Reads need ``task.view``; anything that hands out the
original CSV bytes or text needs ``task.download``.
"""
from __future__ import annotations

import math

from flask import Blueprint, Response, g, request, send_file, stream_with_context

from ...extensions import db
from ...models import CurveView, Task
from ...services import curve_context_service, task_curves_service as curves, task_service
from ...services.lanmatrix import permissions
from ...services.lanmatrix.service import ServiceError
from ._base import _project_and_role, err, login_required, ok, register_common

bp = Blueprint("lanmatrix_curves", __name__, url_prefix="/api/v1")
register_common(bp)

@bp.errorhandler(curves.CurveError)
def _curve_error(exc):
    response, status = err(exc.code, str(exc), status=exc.status)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _task(project_id, task_key, capability="task.view"):
    project, role = _project_and_role(project_id, capability)
    task = task_service.get_project_task(project_id, task_key)
    if task is None:
        raise ServiceError("任务不存在", code="NOT_FOUND")
    return project, role, task


def _arg_number(name, default=None, *, integer=False):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw) if integer else float(raw)
    except (ValueError, OverflowError):
        raise curves.CurveError(f"Invalid {name}", "CSV_OPTIONS", 400) from None
    if not integer and not math.isfinite(value):
        raise curves.CurveError(f"Invalid {name}", "CSV_OPTIONS", 400)
    return value


def _columns(required=True):
    raw = request.args.get("columns", "")
    idents = [c for c in raw.split(",") if c] if raw else []
    if required and not idents:
        raise curves.CurveError("Select at least one variable", "CSV_OPTIONS", 400)
    return idents


def _source(task):
    """The requested attempt; the current attempt of a running task is live."""
    run = _arg_number("run", None, integer=True)
    live = request.args.get("live") == "1" or (
        run in (None, task.run_count or 1) and task.status == "running")
    return curves.resolve(task, run, live=live,
                          expected_revision=None if live else request.args.get("revision") or None)


def _ok(data):
    response, status = ok(data)
    response.headers["Cache-Control"] = "no-store"
    return response, status


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves")
@login_required
def curve_context(project_id, task_key):
    """Index metadata, attempts, judge timeline and signal dictionary."""
    _, role, task = _task(project_id, task_key)
    source = _source(task)
    idx = curves.index(source)
    lim = curves.limits()
    judge = curve_context_service.timeline(task, source.run_count, live=source.live)
    return _ok({
        **idx.metadata(lim.max_lanes),
        "task": {"task_key": task.task_key, "test_id": task.test_id, "status": task.status,
                 "run": source.run_count, "current_run": task.run_count or 1},
        "attempts": curves.attempts(task),
        "judge": judge,
        "dictionary": curve_context_service.dictionary(task, source.run_count, judge=judge),
        "can_download": permissions.can("task.download", role, is_system_admin=g.user.is_system_admin),
    })


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/series")
@login_required
def curve_series(project_id, task_key):
    _, _, task = _task(project_id, task_key)
    source = _source(task)
    lim = curves.limits()
    return _ok(curves.index(source).series(
        _columns(), start=_arg_number("start"), end=_arg_number("end"),
        width=_arg_number("width", 1600, integer=True),
        max_lanes=lim.max_lanes, point_limit=lim.point_limit))


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/values")
@login_required
def curve_values(project_id, task_key):
    _, _, task = _task(project_id, task_key)
    at = _arg_number("at")
    if at is None:
        raise curves.CurveError("Cursor time is required", "CSV_OPTIONS", 400)
    return _ok(curves.index(_source(task)).values_at(_columns(), at, curves.limits().max_lanes))


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/edge")
@login_required
def curve_edge(project_id, task_key):
    _, _, task = _task(project_id, task_key)
    at = _arg_number("at")
    direction = _arg_number("dir", 1, integer=True)
    if at is None or direction not in (1, -1):
        raise curves.CurveError("Edge search needs at and dir=1|-1", "CSV_OPTIONS", 400)
    return _ok(curves.index(_source(task)).edge(request.args.get("column", ""), at, direction))


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/events")
@login_required
def curve_events(project_id, task_key):
    _, _, task = _task(project_id, task_key)
    return _ok(curves.index(_source(task)).events(
        _columns(required=False) or None, start=_arg_number("start"), end=_arg_number("end"),
        offset=_arg_number("offset", 0, integer=True),
        limit=_arg_number("limit", curves.EVENT_PAGE, integer=True)))


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/compare")
@login_required
def curve_compare(project_id, task_key):
    """First divergence against another attempt or another task of the project."""
    _, _, task = _task(project_id, task_key)
    other_key = request.args.get("other") or task_key
    _, _, other = _task(project_id, other_key)
    mine = curves.index(_source(task))
    other_source = curves.resolve(other, _arg_number("other_run", None, integer=True))
    if other.id == task.id and other_source.run_count == (_arg_number("run", None, integer=True)
                                                          or task.run_count or 1):
        raise curves.CurveError("Choose a different attempt or task to compare", "CSV_OPTIONS", 400)
    theirs = curves.index(other_source)
    result = mine.divergence(theirs, _columns(required=False) or None)
    result["other"] = {"task_key": other.task_key, "test_id": other.test_id,
                       "run": other_source.run_count, "revision": other_source.revision,
                       "columns": [{"id": c.id, "name": c.name} for c in theirs.columns]}
    return _ok(result)


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/raw")
@login_required
def curve_raw(project_id, task_key):
    _, _, task = _task(project_id, task_key, "task.download")
    return _ok(curves.raw(_source(task), offset=_arg_number("offset", 0, integer=True),
                          count=_arg_number("count", curves.RAW_PAGE, integer=True)))


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/download")
@login_required
def curve_download(project_id, task_key):
    _, _, task = _task(project_id, task_key, "task.download")
    source = _source(task)
    response = send_file(source.path, mimetype="text/csv", as_attachment=True,
                         download_name=f"{task.task_key}_run{source.run_count}_output.csv",
                         conditional=False)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/projects/<int:project_id>/tasks/<task_key>/curves/export")
@login_required
def curve_export(project_id, task_key):
    """Original CSV text of the chosen variables inside a time range."""
    _, _, task = _task(project_id, task_key, "task.download")
    source = _source(task)
    idx = curves.index(source)
    chunks = curves.export_csv(source, idx, _columns(), _arg_number("start"), _arg_number("end"),
                               curves.limits().max_lanes)
    first = next(chunks)  # validate the selection before streaming starts
    response = Response(stream_with_context(_chain(first, chunks)), mimetype="text/csv")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{task.task_key}_run{source.run_count}_selection.csv"')
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _chain(first, rest):
    yield first
    yield from rest


# --------------------------------------------------------------------------- #
# Saved views (per project + test case, shared by all members)
# --------------------------------------------------------------------------- #
_VIEW_KEYS = {"lanes", "range", "group"}


def _layout(body):
    layout = body.get("layout")
    if not isinstance(layout, dict) or set(layout) - _VIEW_KEYS:
        raise ServiceError("视图布局格式无效", code="VALIDATION_ERROR")
    lanes = layout.get("lanes")
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= curves.limits().max_lanes:
        raise ServiceError("视图至少包含一个变量，且不超过泳道上限", code="VALIDATION_ERROR")
    clean = []
    for lane in lanes:
        if not isinstance(lane, dict) or not isinstance(lane.get("name"), str) or not lane["name"].strip():
            raise ServiceError("泳道必须指定变量名", code="VALIDATION_ERROR")
        height = lane.get("height")
        if height is not None and (not isinstance(height, int) or not 24 <= height <= 400):
            raise ServiceError("泳道高度需在 24–400 之间", code="VALIDATION_ERROR")
        clean.append({"name": lane["name"][:512], **({"height": height} if height else {})})
    out = {"lanes": clean}
    span = layout.get("range")
    if span is not None:
        if (not isinstance(span, list) or len(span) != 2
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in span) or span[0] > span[1]):
            raise ServiceError("视图时间范围无效", code="VALIDATION_ERROR")
        out["range"] = span
    return out


def _view_name(body):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise ServiceError("视图名称需为 1–80 个字符", code="VALIDATION_ERROR")
    return name.strip()


def _payload():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ServiceError("请求体必须是 JSON 对象", code="VALIDATION_ERROR")
    return body


@bp.get("/projects/<int:project_id>/curve-views")
@login_required
def list_curve_views(project_id):
    _project_and_role(project_id, "task.view")
    test_id = request.args.get("test_id", "")
    rows = (CurveView.query.filter_by(project_id=project_id, test_id=test_id)
            .order_by(CurveView.name.asc()).all())
    return ok({"views": [r.to_dict() for r in rows]})


@bp.post("/projects/<int:project_id>/curve-views")
@login_required
def create_curve_view(project_id):
    _project_and_role(project_id, "task.upload")
    body = _payload()
    test_id = body.get("test_id")
    if not isinstance(test_id, str) or not test_id or len(test_id) > 128:
        raise ServiceError("缺少 test_id", code="VALIDATION_ERROR")
    if not Task.query.filter_by(project_id=project_id, test_id=test_id, deleted_at=None).first():
        raise ServiceError("该项目没有这个测试用例的任务", code="NOT_FOUND")
    name = _view_name(body)
    if CurveView.query.filter_by(project_id=project_id, test_id=test_id, name=name).first():
        raise ServiceError("同名视图已存在", code="DUPLICATE")
    view = CurveView(project_id=project_id, test_id=test_id, name=name, layout=_layout(body),
                     created_by=g.user.id)
    db.session.add(view)
    db.session.commit()
    return ok({"view": view.to_dict()}, status=201)


def _owned_view(project_id, view_id):
    _, role = _project_and_role(project_id, "task.upload")
    view = CurveView.query.filter_by(id=view_id, project_id=project_id).first()
    if view is None:
        raise ServiceError("视图不存在", code="NOT_FOUND")
    if view.created_by != g.user.id and not (g.user.is_system_admin or role == "project_admin"):
        raise permissions.PermissionDenied("curve_view.edit")
    return view


@bp.put("/projects/<int:project_id>/curve-views/<int:view_id>")
@login_required
def update_curve_view(project_id, view_id):
    view = _owned_view(project_id, view_id)
    body = _payload()
    if "name" in body:
        name = _view_name(body)
        clash = CurveView.query.filter_by(project_id=project_id, test_id=view.test_id, name=name).first()
        if clash is not None and clash.id != view.id:
            raise ServiceError("同名视图已存在", code="DUPLICATE")
        view.name = name
    if "layout" in body:
        view.layout = _layout(body)
    db.session.commit()
    return ok({"view": view.to_dict()})


@bp.delete("/projects/<int:project_id>/curve-views/<int:view_id>")
@login_required
def delete_curve_view(project_id, view_id):
    view = _owned_view(project_id, view_id)
    db.session.delete(view)
    db.session.commit()
    return ok({"deleted": True})
