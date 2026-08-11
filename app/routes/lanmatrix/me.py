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
from ...models import LMUser, ProjectMember, Task, TaskStatus
from ...services import task_service
from ...services.lanmatrix import (notification_service, permissions,
                                   review_service, service)
from ._base import arg_int, err, ok, login_required, register_common

bp = Blueprint("lanmatrix_me", __name__, url_prefix="/api/v1")
register_common(bp)

# Cross-project listing is a triage view, not an archive. A hard ceiling keeps
# one query from dragging the whole task table into memory for an admin who can
# see every project.
_MAX_TASKS = 300


def _visible_projects() -> list:
    """Projects the current user may read. System admins see all."""
    return service.list_projects(g.user)


def _roles_in(project_ids: list[int]) -> dict[int, str]:
    """Role of the current user in each project, in one query.

    ``users_service.role_in_project`` costs a query per project, and the
    workspace resolves roles for every visible project on every load. A system
    admin sees them all, so per-project lookups would turn one page load into
    dozens of round trips.
    """
    if not project_ids:
        return {}
    if g.user.is_system_admin:
        return {pid: "project_admin" for pid in project_ids}
    rows = (
        db.session.query(ProjectMember.project_id, ProjectMember.role)
        .filter(ProjectMember.project_id.in_(project_ids),
                ProjectMember.user_id == g.user.id)
        .all()
    )
    return {pid: role for pid, role in rows}


def _capabilities(role: str | None) -> dict:
    """What the current user may do to tasks in a project.

    Shipped per project because the workspace list spans several of them: a user
    who is admin of one project and a reader in another must see 删除 on the
    first project's rows only. Sending a single flag for the whole page would
    either hide a button the user is entitled to or show one the server will
    refuse.
    """
    admin = g.user.is_system_admin
    return {
        "can_delete": permissions.can("task.delete", role, is_system_admin=admin),
        "can_cancel": permissions.can("task.cancel", role, is_system_admin=admin),
        "can_upload": permissions.can("task.upload", role, is_system_admin=admin),
        "can_download": permissions.can("task.download", role, is_system_admin=admin),
    }


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
    roles = _roles_in(sorted(seen))
    # Capabilities ride along per project so the workspace list can offer the
    # same verbs as the project task list without asking one endpoint per row's
    # project -- and without guessing, which is how a 删除 button ends up shown
    # to somebody the server will refuse.
    payload = []
    for pid in sorted(seen):
        entry = {"id": pid, "code": by_id[pid].code, "name": by_id[pid].name,
                 "role": roles.get(pid)}
        entry.update(_capabilities(roles.get(pid)))
        payload.append(entry)
    # Same review projection the project task list uses, so the two lists cannot
    # disagree about whether a run's verdict has been signed off.
    reviews = review_service.reviews_for_tasks(rows)
    reviewer_ids = review_service.task_review_user_ids(reviews)
    if reviewer_ids:
        users = {u.id: u for u in
                 LMUser.query.filter(LMUser.id.in_(reviewer_ids)).all()}
        for entry in reviews.values():
            user = users.get(entry.get("reviewer_id"))
            if user:
                entry["reviewer_name"] = user.display_name or user.username or ""
    tasks = review_service.attach_reviews(rows, [t.to_dict() for t in rows],
                                          reviews)
    return ok({
        "tasks": tasks,
        "projects": payload,
        "truncated": truncated,
    })


# --------------------------------------------------------------------------- #
# Review queue
#
# The reason this lives beside the task list rather than inside a project: an
# assigned review is work the reviewer did not choose and may not know about, so
# it has to appear where they already look. A review queue you have to go
# hunting for per project is a review queue nobody clears.
#
# Visibility reuses ``_visible_projects`` unchanged, so the review queue cannot
# leak a row from a project the user cannot already read.
# --------------------------------------------------------------------------- #
@bp.get("/me/reviews")
@login_required
def me_reviews():
    projects = _visible_projects()
    by_id = {p.id: p for p in projects}
    if not by_id:
        return ok({"reviews": [], "projects": [], "counts": {}})

    wanted = arg_int("project_id", None)
    pids = [wanted] if wanted in by_id else list(by_id)

    # ``role`` + ``status`` turn the one-purpose pending queue into the three
    # views the workflow actually has: work assigned to me, decisions made on my
    # submissions (above all the rejections), and my own decided history. A
    # decided row leaves the reviewer's queue by definition, so before this the
    # product had nowhere at all to see what had been rejected.
    role = (request.args.get("role") or review_service.ROLE_REVIEWER).strip()
    status = (request.args.get("status") or review_service.PENDING).strip()

    limit = arg_int("limit", 200, minimum=1, maximum=500)
    rows = review_service.queue_for(g.user.id, pids, status=status, role=role,
                                    limit=limit)

    user_ids = review_service.review_user_ids(rows)
    users = {u.id: u for u in
             LMUser.query.filter(LMUser.id.in_(user_ids)).all()} \
        if user_ids else {}

    seen = {r.project_id for r in rows if r.project_id in by_id}
    return ok({
        "reviews": [review_service.row_review_dict(r, users) for r in rows],
        "projects": [{"id": pid, "code": by_id[pid].code, "name": by_id[pid].name}
                     for pid in sorted(seen)],
        "counts": review_service.counts_for(list(by_id)),
        # Tab counters: how many rows each of the three views holds.
        "queue_counts": review_service.counts_by_role(g.user.id, list(by_id)),
        "role": role,
        "status": status,
    })


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def _notification_totals() -> dict:
    """Both tab counters in one place, so a response cannot ship half of them."""
    return {
        "unread": notification_service.unread_count(g.user.id),
        "history": notification_service.history_count(g.user.id),
    }


@bp.get("/me/notifications")
@login_required
def me_notifications():
    """List notifications for one scope (``unread`` | ``history`` | ``all``).

    The legacy ``?unread=1`` flag still works so an older cached page keeps
    functioning after a deploy.
    """
    scope = (request.args.get("scope") or "").strip().lower()
    if scope not in notification_service.SCOPES:
        scope = notification_service.SCOPE_UNREAD
    limit = arg_int("limit", None, minimum=1, maximum=200)
    rows = notification_service.list_for(g.user.id, scope=scope, limit=limit)
    payload = {"notifications": [n.to_dict() for n in rows], "scope": scope}
    payload.update(_notification_totals())
    return ok(payload)


@bp.get("/me/notifications/unread_count")
@login_required
def me_notifications_unread_count():
    """Cheap endpoint for the 30s badge poll -- one COUNT, no row payload."""
    return ok({"unread": notification_service.unread_count(g.user.id)})


@bp.post("/me/notifications/archive")
@login_required
def me_notifications_archive():
    """File notifications away into history.

    Archiving is not deleting: "I have dealt with this" and "this never
    happened" are different statements, and only the first one is safe to make
    on the user's behalf. Archived rows also stop being revived by the
    collapsing window, which is what made dismissed items reappear.
    """
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if ids is not None and not isinstance(ids, list):
        return err("INVALID_ARGUMENT", "ids 必须是数组", status=400)
    changed = notification_service.archive(g.user.id, ids)
    body = {"archived": changed}
    body.update(_notification_totals())
    return ok(body)


@bp.post("/me/notifications/clear_history")
@login_required
def me_notifications_clear_history():
    """Drop read/archived rows only. Unread rows are outstanding work and stay."""
    removed = notification_service.clear_history(g.user.id)
    body = {"removed": removed}
    body.update(_notification_totals())
    return ok(body)


@bp.post("/me/notifications/read")
@login_required
def me_notifications_read():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if ids is not None and not isinstance(ids, list):
        return err("INVALID_ARGUMENT", "ids 必须是数组", status=400)
    changed = notification_service.mark_read(g.user.id, ids)
    return ok({"marked": changed,
               "unread": notification_service.unread_count(g.user.id)})
