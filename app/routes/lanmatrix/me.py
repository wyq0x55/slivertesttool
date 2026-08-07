"""Cross-project ("my workspace") endpoints for the LAN Test Matrix API.

Every other read endpoint is scoped to a single project, which forces the user
to remember where a task lives before they can look at it. These two endpoints
answer the questions a tester actually opens the tool with -- "what is running
right now?" and "what of mine failed?" -- without picking a project first.

Scoping rule (the security-relevant part of this module): a user may only see
tasks belonging to projects they can already see. That set is resolved by
``service.list_projects``, the same function the project list page uses, so
this module cannot drift into a wider view than the rest of the product.
"""

from __future__ import annotations

from flask import Blueprint, g, request

from ...extensions import db
from ...models import Task, TaskStatus
from ...services import task_service
from ...services.lanmatrix import service
from ._base import ok, login_required, register_common

bp = Blueprint("lanmatrix_me", __name__, url_prefix="/api/v1")
register_common(bp)

# Cross-project listing is a triage view, not an archive. A hard ceiling keeps
# one query from dragging the whole task table into memory for an admin who can
# see every project.
_MAX_TASKS = 300


def _visible_projects() -> list:
    """Projects the current user may read. System admins see all."""
    return service.list_projects(g.user)


def _status_counts(project_ids: list[int]) -> dict[int, dict[str, int]]:
    """One grouped query -> per-project counts keyed by status.

    Both the KPI band and the per-project rows are derived from this, so the
    headline numbers and the list can never disagree.
    """
    out: dict[int, dict[str, int]] = {}
    if not project_ids:
        return out
    rows = (
        db.session.query(Task.project_id, Task.status, db.func.count(Task.id))
        .filter(Task.project_id.in_(project_ids))
        .group_by(Task.project_id, Task.status)
        .all()
    )
    for pid, status, count in rows:
        out.setdefault(pid, {})[status] = count
    return out


@bp.get("/me/overview")
@login_required
def me_overview():
    """KPI band + the user's projects with their task mix."""
    projects = _visible_projects()
    pids = [p.id for p in projects]
    counts = _status_counts(pids)

    kpi = {"projects": len(projects), "queued": 0, "running": 0,
           "passed": 0, "failed": 0, "cancelled": 0, "total": 0}
    payload = []
    for p in projects:
        c = counts.get(p.id, {})
        total = sum(c.values())
        row = p.to_dict()
        row.update({
            "task_total": total,
            "task_queued": c.get(TaskStatus.QUEUED.value, 0),
            "task_running": c.get(TaskStatus.RUNNING.value, 0),
            "task_passed": c.get(TaskStatus.PASSED.value, 0),
            "task_failed": c.get(TaskStatus.FAILED.value, 0),
        })
        payload.append(row)
        for key in ("queued", "running", "passed", "failed", "cancelled"):
            kpi[key] += c.get(getattr(TaskStatus, key.upper()).value, 0)
        kpi["total"] += total

    # "Mine" counts the authenticated account (submitter_id), never the free-text
    # ``submitter`` label -- that label is display-only and not unique, so
    # matching on it would show one user another user's work.
    mine_open = 0
    if pids:
        mine_open = (
            task_service.live(Task.query)
            .filter(Task.project_id.in_(pids),
                    Task.submitter_id == g.user.id,
                    Task.status.in_([TaskStatus.QUEUED.value,
                                     TaskStatus.RUNNING.value]))
            .count()
        )
    kpi["mine_open"] = mine_open

    return ok({"kpi": kpi, "projects": payload})


@bp.get("/me/tasks")
@login_required
def me_tasks():
    """Tasks across every project the user can see.

    Query params: ``status``, ``mine=1``, ``project_id``, ``q``, ``limit``.
    """
    projects = _visible_projects()
    by_id = {p.id: p for p in projects}
    pids = list(by_id)
    if not pids:
        return ok({"tasks": [], "projects": [], "truncated": False})

    q = task_service.live(Task.query).filter(Task.project_id.in_(pids))

    status = (request.args.get("status") or "").strip()
    if status:
        q = q.filter(Task.status == status)

    if request.args.get("mine") in ("1", "true", "yes"):
        q = q.filter(Task.submitter_id == g.user.id)

    # An explicit project filter must still be intersected with the visible set
    # above, so passing a project id the user cannot see yields nothing rather
    # than that project's tasks.
    raw_pid = request.args.get("project_id")
    if raw_pid:
        try:
            want = int(raw_pid)
        except (TypeError, ValueError):
            want = None
        if want in by_id:
            q = q.filter(Task.project_id == want)
        else:
            return ok({"tasks": [], "projects": [], "truncated": False})

    text = (request.args.get("q") or "").strip()
    if text:
        like = "%" + text.replace("%", r"\%").replace("_", r"\_") + "%"
        q = q.filter(db.or_(Task.task_key.ilike(like),
                            Task.test_id.ilike(like),
                            Task.task_name.ilike(like)))

    try:
        limit = int(request.args.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, _MAX_TASKS))

    # Fetch one extra row to tell "exactly at the limit" from "there is more",
    # so the UI can say so instead of silently hiding results.
    rows = q.order_by(Task.id.desc()).limit(limit + 1).all()
    truncated = len(rows) > limit
    rows = rows[:limit]

    # Ship the project lookup with the payload: a cross-project list is unusable
    # if every row only carries a numeric project_id.
    seen = {t.project_id for t in rows if t.project_id in by_id}
    return ok({
        "tasks": [t.to_dict() for t in rows],
        "projects": [{"id": pid, "code": by_id[pid].code, "name": by_id[pid].name}
                     for pid in sorted(seen)],
        "truncated": truncated,
    })
