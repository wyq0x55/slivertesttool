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
from . import review_routes
from . import silver_json_export as sje

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
    """Decide who reviews this row.

    Chain: explicit -> row's own reviewer -> テスト区分 rule -> project default
    -> project owner.

    The 区分 step is the one that matches how the work is actually divided. A
    test matrix is partitioned by テスト区分 and each 区分 has its own feature
    owner; routing an entire project to one default reviewer makes that person
    either a bottleneck or a rubber stamp. The default reviewer is kept as the
    fallback for 区分 no rule claims.

    A hand-assigned ``row.reviewer_id`` still wins over the rules: someone
    deliberately named a reviewer for this case, and a config change should not
    silently take it away from them.

    Falling back to the project owner as the last resort is deliberate: an
    unassigned review is invisible work, and the owner is the one person
    guaranteed to exist and to be able to reassign it.
    """
    if explicit:
        return int(explicit)
    if row is not None and row.reviewer_id:
        return int(row.reviewer_id)
    if row is not None:
        routed = review_routes.match_reviewer(
            getattr(project, "review_routes", None) or [],
            review_routes.row_category(row),
        )
        if routed:
            return int(routed)
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
    always has a recipient: a row's own reviewer wins, then the テスト区分
    routing rule, then the project's default reviewer, then the owner.
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
    # Remember who produced the verdict. This is the only reliable way to reach
    # them when the review is decided -- see the field's comment on the model.
    if actor_id:
        row.review_requested_by = int(actor_id)

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
    #
    # ``review_requested_by`` first, and it matters: the run write-back stamps
    # evidence onto the row without setting ``updated_by``, so that column holds
    # whoever last hand-edited the matrix. That is very often the reviewer, and
    # notifying the reviewer of their own decision is suppressed as a
    # self-notification -- which is how "审核完了在 test 上没有任何反馈" happens.
    # The older columns stay as the fallback for rows whose review was raised
    # before this field existed.
    target = row.review_requested_by or row.updated_by or row.created_by
    if target:
        kind = notify_svc.REVIEW_APPROVED if approve else notify_svc.REVIEW_REJECTED
        verb = "已通过" if approve else "被驳回"
        label = row.get_field("test_id") or row.case_id or row.title or "case"
        notify_svc.notify(
            target, kind,
            f"审核{verb}：{label}",
            # The rejection reason is the whole point of the notification: it is
            # what the executor has to act on. Approvals rarely carry a note, so
            # they fall back to naming the verdict that was signed off.
            body=(f"驳回理由：{note}" if (note and not approve)
                  else note or f"判定 {row.review_verdict} {verb}。"),
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
#: Queue roles. ``reviewer`` is "rows I must decide on", ``requester`` is "rows
#: I submitted and am waiting on / was told about". The second one is the entire
#: answer to "where do I see what got rejected": a decided row leaves the
#: reviewer's queue by definition, so without this view it leaves the product.
ROLE_REVIEWER = "reviewer"
ROLE_REQUESTER = "requester"
ROLES = (ROLE_REVIEWER, ROLE_REQUESTER)

#: Listing scopes for a queue. ``decided`` is approved+rejected.
STATUS_ALL = "all"
STATUS_DECIDED = "decided"
STATUSES = (PENDING, APPROVED, REJECTED, STATUS_DECIDED, STATUS_ALL)


def queue_for(user_id: int, project_ids: Iterable[int], *,
              status: str = PENDING, role: str = ROLE_REVIEWER,
              limit: int = 200) -> list[TestItemRow]:
    """Review rows for one user, newest activity first.

    ``role`` picks which side of the review the user is on and ``status`` picks
    which states to include. Both default to the historical behaviour of
    :func:`pending_for` (my pending queue) so existing callers are unchanged.

    Ordering is by the timestamp that actually moved: a decided row sorts by
    ``reviewed_at``, an outstanding one by ``review_requested_at``. Sorting
    everything by the request time buries a decision that just landed on a row
    submitted last week.
    """
    pids = [int(p) for p in project_ids]
    if not pids or not user_id:
        return []
    status = status if status in STATUSES else PENDING
    role = role if role in ROLES else ROLE_REVIEWER

    q = (TestItemRow.query
         .filter(TestItemRow.project_id.in_(pids))
         .filter(TestItemRow.sheet == "test")
         .filter(TestItemRow.deleted_at.is_(None))
         .filter(TestItemRow.review_status != NONE))

    if role == ROLE_REVIEWER:
        q = q.filter(TestItemRow.reviewer_id == user_id)
    else:
        # Rows raised before ``review_requested_by`` existed fall back to the
        # authorship columns, the same chain :func:`decide` notifies through, so
        # the list and the bell cannot disagree about whose row this is.
        q = q.filter(db.or_(TestItemRow.review_requested_by == user_id,
                            db.and_(TestItemRow.review_requested_by.is_(None),
                                    db.or_(TestItemRow.updated_by == user_id,
                                           TestItemRow.created_by == user_id))))

    if status == STATUS_DECIDED:
        q = q.filter(TestItemRow.review_status.in_((APPROVED, REJECTED)))
    elif status != STATUS_ALL:
        q = q.filter(TestItemRow.review_status == status)

    activity = db.func.coalesce(TestItemRow.reviewed_at,
                                TestItemRow.review_requested_at)
    return (q.order_by(db.nullslast(activity.desc()), TestItemRow.id.desc())
             .limit(max(1, min(limit, 500)))
             .all())


def pending_for(user_id: int, project_ids: Iterable[int],
                limit: int = 200) -> list[TestItemRow]:
    """Rows awaiting this user's review, newest request first."""
    return queue_for(user_id, project_ids, status=PENDING,
                     role=ROLE_REVIEWER, limit=limit)


def counts_by_role(user_id: int, project_ids: Iterable[int]) -> dict:
    """Tab counters for the workspace review panel.

    One grouped query per role rather than one per tab: the panel shows three
    numbers and must not cost three scans of the matrix.
    """
    out = {"pending": 0, "rejected": 0, "decided": 0}
    pids = [int(p) for p in project_ids]
    if not pids or not user_id:
        return out
    mine_requested = db.or_(
        TestItemRow.review_requested_by == user_id,
        db.and_(TestItemRow.review_requested_by.is_(None),
                db.or_(TestItemRow.updated_by == user_id,
                       TestItemRow.created_by == user_id)))
    base = (db.session.query(TestItemRow.review_status,
                             db.func.count(TestItemRow.id))
            .filter(TestItemRow.project_id.in_(pids))
            .filter(TestItemRow.sheet == "test")
            .filter(TestItemRow.deleted_at.is_(None))
            .filter(TestItemRow.review_status != NONE))
    for state, count in (base.filter(TestItemRow.reviewer_id == user_id)
                             .group_by(TestItemRow.review_status).all()):
        if state == PENDING:
            out["pending"] = int(count)
        elif state in (APPROVED, REJECTED):
            out["decided"] += int(count)
    rejected = (base.filter(mine_requested)
                    .filter(TestItemRow.review_status == REJECTED)
                    .group_by(TestItemRow.review_status).all())
    out["rejected"] = int(rejected[0][1]) if rejected else 0
    return out


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


def _user_name(users: Optional[dict], user_id) -> str:
    user = (users or {}).get(user_id)
    if not user:
        return ""
    return user.display_name or user.username or ""


def review_user_ids(rows: Iterable[TestItemRow]) -> set:
    """Every user id a queue listing has to resolve to a name, in one set.

    Reviewer *and* requester: a "我被驳回" listing shows who decided it, and a
    reviewer's queue shows who submitted it. Resolving them in two passes is two
    round trips for one page.
    """
    ids = set()
    for row in rows:
        if row.reviewer_id:
            ids.add(row.reviewer_id)
        requester = row.review_requested_by or row.updated_by or row.created_by
        if requester:
            ids.add(requester)
    return ids


def row_review_dict(row: TestItemRow,
                    reviewers: Optional[dict[int, LMUser]] = None) -> dict:
    """The review-facing projection of a row, for the workspace queue.

    Includes ``description`` explicitly: when reviewing an ``Untestable`` case
    the stated reason is the entire thing being reviewed, so a queue that does
    not show it forces the reviewer to open every row to learn anything.

    ``reviewers`` is a plain ``{id: LMUser}`` lookup and may carry requesters
    too -- build it with :func:`review_user_ids`.
    """
    requester_id = row.review_requested_by or row.updated_by or row.created_by
    return {
        "uuid": row.uuid,
        "id": row.id,
        "project_id": row.project_id,
        "case_id": row.case_id,
        # The list is read by test id, not by the synthetic row identity.
        "test_id": sje.row_test_id(row),
        "title": row.title,
        "result": row.result,
        "review_status": row.review_status,
        "review_verdict": row.review_verdict,
        "review_note": row.review_note,
        "reviewer_id": row.reviewer_id,
        "reviewer_name": _user_name(reviewers, row.reviewer_id),
        "requester_id": requester_id,
        "requester_name": _user_name(reviewers, requester_id),
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


# --------------------------------------------------------------------------- #
# Task -> review projection
#
# A run's verdict and that verdict's sign-off live in two different tables
# joined only by ``test_id``: the task list showed the claim and never the
# decision, so a reviewer's approval or rejection was invisible exactly where
# the executor looks. This attaches the decision to the runs that produced it.
# --------------------------------------------------------------------------- #
#: Which review state wins when one test id maps to several matrix rows.
#: Outstanding work outranks a rejection outranks an approval -- the summary
#: must never read "已通过" while one of the rows still blocks the release.
_STATE_RANK = {PENDING: 3, REJECTED: 2, APPROVED: 1, NONE: 0}


def reviews_for_tasks(tasks: Iterable, users: Optional[dict] = None) -> dict:
    """``{(project_id, test_id): review_dict}`` for a page of tasks.

    One query per project regardless of how many tasks are on the page. The
    narrowing predicate mirrors ``run_writeback_service._matching_rows`` (the
    portable ``.as_string()`` comparator, never the JSONB-only ``.astext``) and
    the result is re-confirmed in Python with :func:`sje.row_test_id`, which
    also settles precedence for a row carrying both ``case_id`` and a differing
    ``test_id`` field.

    Returns an empty mapping rather than raising: the task list must still
    render if the matrix cannot be read.
    """
    wanted: dict[int, set] = {}
    for task in tasks:
        pid = getattr(task, "project_id", None)
        tid = (getattr(task, "test_id", "") or "").strip()
        if pid and tid:
            wanted.setdefault(int(pid), set()).add(tid)
    if not wanted:
        return {}

    out: dict = {}
    try:
        for pid, needles in wanted.items():
            ids = list(needles)
            rows = (TestItemRow.query
                    .filter_by(project_id=pid, sheet="test", deleted_at=None)
                    .filter(TestItemRow.review_status != NONE)
                    .filter(db.or_(
                        TestItemRow.case_id.in_(ids),
                        TestItemRow.custom_values["test_id"].as_string().in_(ids)))
                    .all())
            best: dict = {}
            for row in rows:
                tid = sje.row_test_id(row)
                if tid not in needles:
                    continue
                key = (pid, tid)
                prev = best.get(key)
                rank = _STATE_RANK.get(row.review_status, 0)
                seen = (prev[2] if prev else 0) + 1
                if prev is None or rank > prev[0]:
                    best[key] = (rank, row, seen)
                else:
                    best[key] = (prev[0], prev[1], seen)
            for key, (_rank, row, count) in best.items():
                out[key] = task_review_dict(row, count=count, users=users)
    except Exception:  # noqa: BLE001 - a list must not fail over a side panel
        logger.exception("failed to project reviews onto tasks")
        return {}
    return out


def task_review_dict(row: TestItemRow, *, count: int = 1,
                     users: Optional[dict] = None) -> dict:
    """The compact review summary carried by a task row.

    Deliberately smaller than :func:`row_review_dict`: the task list needs the
    state, who decided it, why, and a way to open the row -- not the whole case.
    """
    return {
        "row_uuid": row.uuid,
        "status": row.review_status,
        "verdict": row.review_verdict,
        "note": row.review_note or "",
        "reviewer_id": row.reviewer_id,
        "reviewer_name": _user_name(users, row.reviewer_id),
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "requested_at": (row.review_requested_at.isoformat()
                         if row.review_requested_at else None),
        # >1 means this test id is claimed by several matrix rows and the shown
        # state is the most severe of them, not the only one.
        "rows": int(count or 1),
    }


def task_review_user_ids(reviews: dict) -> set:
    """Reviewer ids referenced by a :func:`reviews_for_tasks` mapping."""
    return {r.get("reviewer_id") for r in reviews.values() if r.get("reviewer_id")}


def attach_reviews(tasks: list, payload: list, reviews: dict) -> list:
    """Merge a review mapping into already-serialised task dicts, in order."""
    for task, data in zip(tasks, payload):
        pid = getattr(task, "project_id", None)
        tid = (getattr(task, "test_id", "") or "").strip()
        data["review"] = reviews.get((int(pid), tid)) if pid and tid else None
    return payload
