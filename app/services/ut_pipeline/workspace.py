"""Unit-test jobs in the cross-project workspace.

The workspace review queue is one list for both project types. A Silver row
is a verdict assigned to one named reviewer; a unit-test job waiting at a
human gate is open to every member allowed to act on it, so it is listed for
each of them. Rows are projected into the review-queue shape with
``kind = "ut_job"`` so the client can route the action to the job detail,
where the candidate CSV is inspected and the decision is pinned to it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from ...extensions import db
from ...models import LMUser, UtBatch, UtJob, UtTarget
from ..lanmatrix import permissions
from ..lanmatrix.review_service import (APPROVED, PENDING, REJECTED, ROLE_REQUESTER,
                                        STATUS_ALL, STATUS_DECIDED)

STATE_ZH = {
    UtJob.AWAITING_REVIEW: "待审核", UtJob.NEEDS_HUMAN: "需人工",
    UtJob.BLOCKED: "环境阻塞", UtJob.ERROR: "异常",
    UtJob.APPROVED: "审核通过", UtJob.REJECTED: "审核驳回",
}
# Gates an editor can clear (revision / retry) without the review capability.
_EDITOR_GATES = (UtJob.NEEDS_HUMAN, UtJob.BLOCKED, UtJob.ERROR)


def actionable_states(role: Optional[str], is_admin: bool) -> tuple[str, ...]:
    """Gate states this role is expected to work on."""
    if permissions.can("item.review", role, is_system_admin=is_admin):
        return UtJob.HUMAN_GATES
    if permissions.can("item.edit", role, is_system_admin=is_admin):
        return _EDITOR_GATES
    return ()


def _pending_filter(user: LMUser, roles: Mapping[int, Optional[str]]):
    clauses = []
    for pid, role in roles.items():
        states = actionable_states(role, user.is_system_admin)
        if states:
            clauses.append(db.and_(UtJob.project_id == pid, UtJob.state.in_(states)))
    return db.or_(*clauses) if clauses else None


def _final_states(status: str) -> tuple[str, ...]:
    if status == APPROVED:
        return (UtJob.APPROVED,)
    if status == REJECTED:
        return (UtJob.REJECTED,)
    return UtJob.FINAL


def _scoped(user: LMUser, roles: Mapping[int, Optional[str]], status: str, role: str):
    """The job query for one workspace scope, or None when it is empty."""
    pids = list(roles)
    if not pids:
        return None
    query = (db.session.query(UtJob, UtBatch.created_by)
             .join(UtBatch, UtBatch.id == UtJob.batch_id)
             .filter(UtJob.project_id.in_(pids)))
    if role == ROLE_REQUESTER:
        query = query.filter(UtBatch.created_by == user.id)
        if status == PENDING:
            return query.filter(UtJob.state.in_(UtJob.HUMAN_GATES))
        if status == STATUS_ALL:
            return query.filter(UtJob.state.in_(UtJob.HUMAN_GATES + UtJob.FINAL))
        return query.filter(UtJob.state.in_(_final_states(status)))

    decided = db.and_(UtJob.reviewed_by == user.id, UtJob.state.in_(_final_states(status)))
    if status in (APPROVED, REJECTED, STATUS_DECIDED):
        return query.filter(decided)
    pending = _pending_filter(user, roles)
    if status == STATUS_ALL:
        return query.filter(db.or_(decided, pending) if pending is not None else decided)
    return query.filter(pending) if pending is not None else None


def _name(users: Mapping[int, LMUser], uid: Optional[int]) -> str:
    user = users.get(uid) if uid else None
    return (user.display_name or user.username) if user else ""


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def row_review_dict(job: UtJob, requester_id: Optional[int],
                    users: Mapping[int, LMUser]) -> dict[str, Any]:
    target: Optional[UtTarget] = job.target
    decided = job.state in UtJob.FINAL
    reason = (job.reason or "").strip()
    description = STATE_ZH.get(job.state, job.state) + (f" · {reason}" if reason else "")
    return {
        "kind": "ut_job",
        "uuid": None,
        "id": job.id,
        "project_id": job.project_id,
        "case_id": None,
        "test_id": target.function if target else f"作业 #{job.id}",
        "title": target.source_rel if target else "",
        "category": "",
        "result": job.last_verdict or "",
        "review_status": job.state if decided else PENDING,
        "review_verdict": job.last_verdict or "",
        "review_note": job.review_note or "",
        "reviewer_id": job.reviewed_by,
        "reviewer_name": _name(users, job.reviewed_by),
        "requester_id": requester_id,
        "requester_name": _name(users, requester_id),
        "review_requested_at": _iso(job.updated_at),
        "reviewed_at": _iso(job.reviewed_at),
        "description": description,
        "executor": _name(users, requester_id),
        "exec_date": job.updated_at.strftime("%Y-%m-%d %H:%M") if job.updated_at else "",
        "version_label": "",
        # Approval is pinned to the candidate shown in the job detail, and a
        # machine-failed candidate needs a written opinion, so UT jobs are never
        # decided from the list.
        "needs_note": job.state != UtJob.AWAITING_REVIEW,
        "bulk_approvable": False,
        "job_state": job.state,
        "link": f"/lanmatrix/projects/{job.project_id}/tasks?job={job.id}",
    }


def queue_for(user: LMUser, roles: Mapping[int, Optional[str]], *, status: str,
              role: str, limit: int) -> list[dict[str, Any]]:
    query = _scoped(user, roles, status, role)
    if query is None:
        return []
    rows = query.order_by(UtJob.updated_at.desc(), UtJob.id.desc()).limit(limit).all()
    ids = {uid for job, creator in rows for uid in (job.reviewed_by, creator) if uid}
    users = {u.id: u for u in LMUser.query.filter(LMUser.id.in_(ids))} if ids else {}
    return [row_review_dict(job, creator, users) for job, creator in rows]


def counts_by_role(user: LMUser, roles: Mapping[int, Optional[str]]) -> dict[str, int]:
    """``{pending, rejected, decided}`` with the same meaning as the review queue."""
    out = {"pending": 0, "rejected": 0, "decided": 0}
    for key, status, role in (("pending", PENDING, "reviewer"),
                              ("decided", STATUS_DECIDED, "reviewer"),
                              ("rejected", REJECTED, ROLE_REQUESTER)):
        query = _scoped(user, roles, status, role)
        out[key] = query.count() if query is not None else 0
    return out


def project_counts(project_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """Per-project job mix for workspace project cards (latest job per target)."""
    pids = [int(p) for p in project_ids]
    out = {pid: {"ut_targets": 0, "ut_active": 0, "ut_gates": 0, "ut_approved": 0}
           for pid in pids}
    if not pids:
        return out
    rows = (db.session.query(UtTarget.project_id, UtJob.state, db.func.count(UtTarget.id))
            .outerjoin(UtJob, UtJob.id == UtTarget.latest_job_id)
            .filter(UtTarget.project_id.in_(pids), UtTarget.in_index.is_(True))
            .group_by(UtTarget.project_id, UtJob.state).all())
    for pid, state, count in rows:
        entry = out[pid]
        entry["ut_targets"] += count
        if state in UtJob.ACTIVE:
            entry["ut_active"] += count
        elif state in UtJob.HUMAN_GATES:
            entry["ut_gates"] += count
        elif state == UtJob.APPROVED:
            entry["ut_approved"] += count
    return out
