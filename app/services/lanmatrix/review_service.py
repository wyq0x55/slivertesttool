"""Reviewer sign-off for test verdicts.

Why this exists
---------------
A verdict produced by a run is a *claim*. Two claims in particular are worth a
second pair of eyes before they become accepted evidence:

* **PASS** -- the case is reported as working. On a safety-relevant project this
  is exactly the claim you want somebody else to have looked at.
* **Untestable** -- the case is declared impossible to test. This silently
  removes a case from the evidence base, which makes it the *more* dangerous of
  the two: an unreviewed ``Untestable`` looks like progress on a chart while
  actually being a gap.

Hence the asymmetry in the rules below, and hence ``Untestable`` defaulting to
"review required" while PASS defaults to off.

The batching asymmetry
----------------------
PASS review supports **bulk approval**: a regression sweep can turn 300 cases
green at once, and forcing 300 individual clicks would guarantee the reviewer
starts rubber-stamping -- which is worse than no review, because it produces the
paperwork of diligence without the diligence.

``Untestable`` is deliberately **not** bulk-approvable and requires a written
note, because each one is an individual judgement call about a specific case.

State machine
-------------
::

    ""  --(run produces a reviewable verdict)-->  pending
    pending --(approve)--> approved
    pending --(reject)---> rejected
    rejected --(next run)--> pending

A rejection is not a dead end: it sends the case back to the implementer, and
the next run re-raises the request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from ...extensions import db
from ...models import LMUser, Notification, Project, TestItemRow
from . import notification_service as notify_svc

logger = logging.getLogger(__name__)

# Review states.
NONE = ""
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

#: Verdict -> the policy key in ``Project.review_required_on`` that governs it.
_VERDICT_POLICY = {
    "pass": "pass",
    "passed": "pass",
    "ok": "pass",
    "success": "pass",
    "untestable": "untestable",
}

#: Verdicts a reviewer must justify in writing, and which cannot be bulk
#: approved. Declaring a case untestable is an individual judgement call.
_REQUIRES_NOTE = {"untestable"}


class ReviewError(Exception):
    """Raised when a review transition is not allowed."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def policy_key(verdict: str) -> str:
    """The policy bucket governing ``verdict`` ("" when it needs no review)."""
    return _VERDICT_POLICY.get((verdict or "").strip().lower(), "")


def requires_note(verdict: str) -> bool:
    return policy_key(verdict) in _REQUIRES_NOTE


def is_bulk_approvable(verdict: str) -> bool:
    """Whether rows carrying this verdict may be approved in bulk."""
    key = policy_key(verdict)
    return bool(key) and key not in _REQUIRES_NOTE


def needs_review(project: Project, verdict: str) -> bool:
    """Whether ``verdict`` requires sign-off under the project's policy."""
    key = policy_key(verdict)
    if not key:
        return False
    return bool(project.review_policy().get(key))


# --------------------------------------------------------------------------- #
# Raising a request
# --------------------------------------------------------------------------- #
def resolve_reviewer(project: Project, row: Optional[TestItemRow] = None,
                     explicit: Optional[int] = None) -> Optional[int]:
    """Decide who reviews this row: explicit -> row -> project -> owner.

    Without the last two links this chain terminated at ``row.reviewer_id``,
    which is only ever set by assigning a reviewer to that row by hand. In
    practice nobody does that per row, so every automatic request resolved to
    ``None``: no notification was sent, and ``pending_for`` -- which filters by
    reviewer -- returned an empty queue. Turning review on therefore did
    nothing at all, silently.

    Falling back to the project owner as the last resort is deliberate: an
    unassigned review is invisible work, and the owner is the one person
    guaranteed to exist and to be able to reassign it.
    """
    if explicit:
        return int(explicit)
    if row is not None and row.reviewer_id:
        return int(row.reviewer_id)
    default = getattr(project, "default_reviewer_id", None)
    if default:
        return int(default)
    return int(project.owner_id) if project.owner_id else None


def request_review(project: Project, row: TestItemRow, verdict: str, *,
                   reviewer_id: Optional[int] = None,
                   actor_id: Optional[int] = None) -> bool:
    """Put ``row`` into review for ``verdict`` if the policy demands it.

    Returns True when a request was raised. Idempotent: a row already pending
    for the same verdict is left alone, so re-running a case does not spam the
    assigned reviewer.

    The reviewer is resolved through :func:`resolve_reviewer`, so a request
    always has a recipient: a row's own reviewer wins, then the project's
    default reviewer, then the owner.
    """
    if not needs_review(project, verdict):
        return False

    target = resolve_reviewer(project, row, reviewer_id)
    if (row.review_status == PENDING
            and (row.review_verdict or "") == (verdict or "")
            and row.reviewer_id == target):
        return False

    row.review_status = PENDING
    row.review_verdict = (verdict or "")[:24]
    row.review_requested_at = _utcnow()
    row.reviewed_at = None
    row.reviewer_id = target

    if target:
        notify_svc.notify(
            target, notify_svc.REVIEW_ASSIGNED,
            f"待审核：{row.case_id or row.title or 'case'}",
            body=f"判定为 {verdict}，等待你的审核。",
            project_id=project.id,
            # Open the case itself. A link to the queue makes the reviewer
            # search for the row the notification already named.
            link_url=notify_svc.review_item_link(project.id, row.uuid or ""),
            ref_type="test_item", ref_id=row.uuid or str(row.id),
            # One row per case. Grouping by verdict collapsed a whole sweep into
            # a single "×300" entry whose single link reached exactly one of
            # them -- the other 299 were announced and then unreachable.
            group_key=(f"review.assigned:{project.id}:{policy_key(verdict)}"
                       f":{row.uuid or row.id}"),
            actor_id=actor_id,
        )
    return True


def assign_reviewer(row: TestItemRow, reviewer_id: Optional[int], *,
                    project: Optional[Project] = None,
                    actor_id: Optional[int] = None) -> None:
    """(Re)assign the reviewer of a row, notifying the new one."""
    if row.reviewer_id == reviewer_id:
        return
    row.reviewer_id = reviewer_id
    if reviewer_id and row.review_status == PENDING and project is not None:
        notify_svc.notify(
            reviewer_id, notify_svc.REVIEW_ASSIGNED,
            f"待审核：{row.case_id or row.title or 'case'}",
            body=f"判定为 {row.review_verdict}，等待你的审核。",
            project_id=project.id,
            link_url=notify_svc.review_item_link(project.id, row.uuid or ""),
            ref_type="test_item", ref_id=row.uuid or str(row.id),
            group_key=f"review.assigned:{project.id}:manual:{row.uuid or row.id}",
            actor_id=actor_id,
        )


# --------------------------------------------------------------------------- #
# Deciding
# --------------------------------------------------------------------------- #
def decide(project: Project, row: TestItemRow, approve: bool, *,
           actor_id: int, note: str = "") -> TestItemRow:
    """Approve or reject one pending review.

    A note is mandatory for a rejection (the implementer needs to know what to
    fix) and for anything involving ``Untestable`` (the justification *is* the
    evidence). Raises :class:`ReviewError` otherwise.
    """
    if row.review_status != PENDING:
        raise ReviewError("该用例当前不在待审核状态。")

    note = (note or "").strip()
    if not approve and not note:
        raise ReviewError("驳回时必须填写理由。")
    if approve and requires_note(row.review_verdict) and not note:
        raise ReviewError("审核『无法测试』的用例时必须填写意见。")

    row.review_status = APPROVED if approve else REJECTED
    row.review_note = note
    row.reviewed_at = _utcnow()
    row.version = (row.version or 1) + 1
    row.updated_at = _utcnow()

    # Tell whoever produced the verdict, not the whole project.
    target = row.updated_by or row.created_by
    if target:
        kind = notify_svc.REVIEW_APPROVED if approve else notify_svc.REVIEW_REJECTED
        verb = "已通过" if approve else "被驳回"
        notify_svc.notify(
            target, kind,
            f"审核{verb}：{row.case_id or row.title or 'case'}",
            body=note or f"判定 {row.review_verdict} {verb}。",
            project_id=project.id,
            link_url=notify_svc.review_item_link(project.id, row.uuid or ""),
            ref_type="test_item", ref_id=row.uuid or str(row.id),
            group_key=f"{kind}:{project.id}:{row.uuid or row.id}",
            actor_id=actor_id,
        )
    return row


def decide_bulk(project: Project, rows: Iterable[TestItemRow], approve: bool, *,
                actor_id: int, note: str = "") -> dict:
    """Approve/reject many rows at once.

    Rows whose verdict is not bulk-approvable (``Untestable``) are refused
    individually and reported back in ``skipped`` rather than failing the whole
    call -- a reviewer clearing 300 passes should not be blocked by two
    untestables mixed into the selection.
    """
    done, skipped = [], []
    for row in rows:
        if row.review_status != PENDING:
            skipped.append({"uuid": row.uuid, "reason": "not_pending"})
            continue
        if not is_bulk_approvable(row.review_verdict):
            skipped.append({"uuid": row.uuid, "reason": "needs_individual_review"})
            continue
        try:
            decide(project, row, approve, actor_id=actor_id, note=note)
            done.append(row.uuid)
        except ReviewError as exc:
            skipped.append({"uuid": row.uuid, "reason": str(exc)})
    db.session.commit()
    return {"approved" if approve else "rejected": done, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def pending_for(user_id: int, project_ids: Iterable[int],
                limit: int = 200) -> list[TestItemRow]:
    """Rows awaiting this user's review, newest request first."""
    pids = [int(p) for p in project_ids]
    if not pids or not user_id:
        return []
    return (TestItemRow.query
            .filter(TestItemRow.project_id.in_(pids))
            .filter(TestItemRow.sheet == "test")
            .filter(TestItemRow.deleted_at.is_(None))
            .filter(TestItemRow.review_status == PENDING)
            .filter(TestItemRow.reviewer_id == user_id)
            .order_by(db.nullslast(TestItemRow.review_requested_at.desc()),
                      TestItemRow.id.desc())
            .limit(max(1, min(limit, 500)))
            .all())


def counts_for(project_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """``{project_id: {state: count}}`` over the given projects."""
    pids = [int(p) for p in project_ids]
    if not pids:
        return {}
    rows = (db.session.query(TestItemRow.project_id,
                             TestItemRow.review_status,
                             db.func.count(TestItemRow.id))
            .filter(TestItemRow.project_id.in_(pids))
            .filter(TestItemRow.sheet == "test")
            .filter(TestItemRow.deleted_at.is_(None))
            .filter(TestItemRow.review_status != "")
            .group_by(TestItemRow.project_id, TestItemRow.review_status)
            .all())
    out: dict[int, dict[str, int]] = {}
    for pid, state, count in rows:
        out.setdefault(pid, {})[state] = count
    return out


def row_review_dict(row: TestItemRow,
                    reviewers: Optional[dict[int, LMUser]] = None) -> dict:
    """The review-facing projection of a row, for the workspace queue.

    Includes ``description`` explicitly: when reviewing an ``Untestable`` case
    the stated reason is the entire thing being reviewed, so a queue that does
    not show it forces the reviewer to open every row to learn anything.
    """
    reviewer = (reviewers or {}).get(row.reviewer_id)
    return {
        "uuid": row.uuid,
        "id": row.id,
        "project_id": row.project_id,
        "case_id": row.case_id,
        "title": row.title,
        "result": row.result,
        "review_status": row.review_status,
        "review_verdict": row.review_verdict,
        "review_note": row.review_note,
        "reviewer_id": row.reviewer_id,
        "reviewer_name": (reviewer.display_name or reviewer.username)
                         if reviewer else "",
        "review_requested_at": (row.review_requested_at.isoformat()
                                if row.review_requested_at else None),
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        # The author's stated reason (説明 column) -- the substance of an
        # Untestable review.
        "description": row.get_field("description") or "",
        "executor": row.get_field("executor") or "",
        "exec_date": row.get_field("exec_date") or "",
        "version_label": row.get_field("version_label") or "",
        "needs_note": requires_note(row.review_verdict),
        "bulk_approvable": is_bulk_approvable(row.review_verdict),
    }
