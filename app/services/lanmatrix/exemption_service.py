"""Sign-off for cases declared "not to be created" (項目作成 = 不要).

Why this exists
---------------
``項目作成`` (``item_created``) states whether a case is to be authored at all.
Its ``不要`` value means "this case will never be written, and therefore never
run". That is a *claim that removes work from the plan*, and until now it was a
free-text cell nobody consumed: the row stayed in the denominator as forever
"not run", and no one was ever asked whether the exclusion was legitimate.

Both halves of that are problems, and they pull in opposite directions:

* Counting a 不要 row as outstanding work makes every project look permanently
  incomplete, so the progress number is ignored -- which is the same as having
  no progress number.
* Letting a 不要 row leave the denominator on its own would mean anyone could
  improve the project's percentage by typing two characters into a cell. An
  unreviewed exclusion looks like progress while actually being a gap -- exactly
  the failure mode :mod:`review_service` exists to prevent for ``Untestable``.

So a 不要 claim is *proposed* scope removal, and only an approved one actually
leaves the denominator. See :func:`app.services.lanmatrix.dashboard_service.summary`.

Why this is not ``review_status``
---------------------------------
The two state machines answer different questions -- "is this verdict
trustworthy?" versus "is skipping this case legitimate?" -- and a row can be
subject to both at once. Sharing one column would let a verdict approval
silently grant a scope exemption, and a rejected verdict would revoke one. They
are kept apart for the same reason ``review_status`` was kept out of
``workflow_status``.

Pending is derived, not stored
------------------------------
There is no "submit for exemption" button, and deliberately so: the claim is
made by typing ``不要`` into the cell, and that edit arrives through the collab
document, an Excel import, or the API. Requiring every one of those paths to
remember to enqueue would guarantee that the one nobody patched leaks rows past
the reviewer.

Instead only *decisions* are stored, and "pending" is computed as::

    item_created == 不要  AND  no decision on record for that value

which cannot be bypassed by any write path, present or future.
:func:`sync_pending` then merely stamps a reviewer and notifies; if it never
runs, the row is still pending and still shows up in the queue.

State machine
-------------
::

    (cell set to 不要)  --> pending --(approve)--> approved  -> out of scope
                                   \\-(reject)---> rejected  -> stays in scope

A rejected row stays in the plan. Changing the cell away from ``不要`` withdraws
the claim, and the stored decision goes dormant (see :func:`effective_status`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ...collab import writeback
from ...extensions import db
from ...models import LMUser, Project, ProjectModel, TestItemRow, TestRunRecord
from . import notification_service as notify_svc
from . import review_service
from . import run_writeback_service as rwb
from . import silver_json_export as sje

logger = logging.getLogger(__name__)

#: Field key holding 項目作成 on a row (``custom_values``, not a column).
FIELD_KEY = "item_created"

#: The three values 項目作成 takes. Only ``NOT_REQUIRED`` removes work.
NOT_REQUIRED = "不要"
CREATED = "作成完了"
IN_PROGRESS = "作成中"
VALUES = (NOT_REQUIRED, CREATED, IN_PROGRESS)

#: Decision states. ``PENDING`` is derived and never stored -- see the module
#: docstring -- but it is a legitimate value to *ask* for when listing.
NONE = ""
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
DECIDED = (APPROVED, REJECTED)

#: Listing scopes for the queue.
STATUS_DECIDED = "decided"
STATUS_ALL = "all"
STATUSES = (PENDING, APPROVED, REJECTED, STATUS_DECIDED, STATUS_ALL)

#: Notification kind for a newly routed exemption claim. Reuses the review
#: channel so it lands in the bell the reviewer already watches; the title makes
#: which kind of sign-off it is unambiguous.
NOTIFY_ASSIGNED = notify_svc.REVIEW_ASSIGNED

#: Upper bound on rows one :func:`sync_pending` pass will stamp and announce. A
#: freshly imported matrix can turn thousands of cells into claims at once, and
#: a reviewer whose bell receives thousands of notifications has been handed the
#: same nothing as a reviewer who received none.
MAX_SYNC = 500

#: ``task_key`` prefix for the synthetic run record an approval writes.
#:
#: An approved 不要 has to reach the per-version and burn-up charts, and both are
#: built exclusively from :class:`TestRunRecord` -- a row column alone is
#: invisible there. The prefix marks the record as produced by a sign-off rather
#: than by a runner, which is what makes the write idempotent (approving twice
#: cannot double-count) and reversible (:func:`reset` can find exactly the record
#: it must retract, without touching a real run).
RECORD_PREFIX = "exemption:"


class ExemptionError(Exception):
    """Raised when an exemption transition is not allowed."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Value handling
# --------------------------------------------------------------------------- #
#: Full-width forms and the spacing Excel likes to leave behind. 項目作成 is
#: typed by hand into a spreadsheet, so ``" 不要 "`` and ``"不要"`` are the same
#: statement and must not produce different scope decisions.
_STRIP = " \t\u3000\r\n"


def normalise_value(raw: Any) -> str:
    """Canonical 項目作成 value, or ``""`` when the cell says nothing.

    Anything that is not one of :data:`VALUES` is returned stripped but
    otherwise untouched: an unrecognised value must never be silently coerced
    into ``不要``, because that would remove a case from the plan on the
    strength of a typo.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return ""
    text = str(raw).strip(_STRIP)
    if not text:
        return ""
    # A dash placeholder is "nothing to say here", not a value.
    if text in ("-", "\u2013", "\u2014", "\uff0d"):
        return ""
    return text


def row_value(row: Any) -> str:
    """The normalised 項目作成 of a row, via its field accessor."""
    if row is None:
        return ""
    getter = getattr(row, "get_field", None)
    if callable(getter):
        return normalise_value(getter(FIELD_KEY))
    if isinstance(row, dict):
        return normalise_value(row.get(FIELD_KEY))
    return ""


def is_claim(row: Any) -> bool:
    """Whether this row currently claims to be out of scope."""
    return row_value(row) == NOT_REQUIRED


def status_of(item_created: Any, exempt_status: Any,
              exempt_value: Any) -> str:
    """Exemption state from raw values: pending / approved / rejected / ``""``.

    Pure and column-based so the dashboard can classify thousands of rows from
    a lightweight ``with_entities`` projection instead of hydrating full ORM
    objects, and so the rule itself is testable without a database.

    A stored decision only counts while the claim it was made about is still on
    the row. Editing 項目作成 away from ``不要`` withdraws the claim, and the
    decision goes dormant rather than being erased -- so putting ``不要`` back
    restores the *same* claim together with the answer it already received,
    instead of quietly re-entering the queue as if it had never been judged.
    """
    if normalise_value(item_created) != NOT_REQUIRED:
        return NONE
    stored = (str(exempt_status or "")).strip()
    if stored in DECIDED and (str(exempt_value or "")).strip() == NOT_REQUIRED:
        return stored
    return PENDING


def effective_status(row: Any) -> str:
    """The exemption state of ``row``: pending / approved / rejected / ``""``."""
    if row is None:
        return NONE
    return status_of(row_value(row),
                     getattr(row, "exempt_status", ""),
                     getattr(row, "exempt_value", ""))


def is_out_of_scope(row: Any) -> bool:
    """Whether ``row`` has actually left the plan (claimed *and* approved)."""
    return effective_status(row) == APPROVED


# --------------------------------------------------------------------------- #
# Routing and enqueueing
# --------------------------------------------------------------------------- #
def resolve_reviewer(project: Project, row: Optional[TestItemRow] = None,
                     explicit: Optional[int] = None) -> Optional[int]:
    """Who signs off this exemption.

    Delegates to :func:`review_service.resolve_reviewer`, so an exemption is
    routed by the same テスト区分 rules, project default and owner fallback as a
    verdict. Whoever is trusted to judge a 区分's results is the person with the
    context to judge whether one of its cases may be skipped, and maintaining a
    second routing table would only let the two disagree.
    """
    return review_service.resolve_reviewer(project, row, explicit)


def sync_pending(project: Project, *, actor_id: Optional[int] = None,
                 notify: bool = True, limit: int = MAX_SYNC) -> int:
    """Stamp and announce claims nobody has been told about yet.

    Purely additive housekeeping: pending status is derived, so a row is in the
    queue whether or not this ever runs. All this does is record *when* the
    claim was first seen and *who* it was routed to, and ring the reviewer's
    bell once.

    Callers must commit. Returns the number of rows stamped.
    """
    rows = (TestItemRow.query
            .filter(TestItemRow.project_id == project.id)
            .filter(TestItemRow.sheet == "test")
            .filter(TestItemRow.deleted_at.is_(None))
            .filter(TestItemRow.exempt_requested_at.is_(None))
            .order_by(TestItemRow.id.asc())
            .limit(max(1, min(int(limit), MAX_SYNC)))
            .all())

    stamped = 0
    for row in rows:
        if effective_status(row) != PENDING:
            continue
        row.exempt_requested_at = _utcnow()
        if actor_id:
            row.exempt_requested_by = int(actor_id)
        target = row.exempt_reviewer_id or resolve_reviewer(project, row)
        row.exempt_reviewer_id = target
        stamped += 1
        if notify and target:
            label = row.get_field("test_id") or row.case_id or row.title or "case"
            notify_svc.notify(
                target, NOTIFY_ASSIGNED,
                f"待审批（項目作成=不要）：{label}",
                body="该用例被标记为不需要作成，等待你的审批。通过后将不计入要实施数。",
                project_id=project.id,
                # The queue, not the row: 通过 / 驳回 live there, and a
                # notification that opens a screen with no way to answer it is
                # a dead end.
                link_url=notify_svc.exemption_queue_link(project.id),
                ref_type="test_item", ref_id=row.uuid or str(row.id),
                group_key=f"exemption.assigned:{project.id}:{row.uuid or row.id}",
                actor_id=actor_id,
            )
    if stamped:
        logger.info("exemption: routed %d new 不要 claim(s) in project %s",
                    stamped, project.id)
    return stamped


# --------------------------------------------------------------------------- #
# Verdict write-back
# --------------------------------------------------------------------------- #
def _current_model_version(project: Project) -> str:
    """Release label to stamp on an exemption, or ``""`` when unknown.

    An exemption is a statement about the *current* build ("this case cannot be
    written for this model"), so it is charted against the newest live model.
    Guessing is worse than admitting ignorance here: an empty label lands in the
    chart's 未标注 bucket, whereas a wrong one silently attributes the decision
    to a release it was never made about.
    """
    model = (ProjectModel.query
             .filter_by(project_id=project.id)
             .filter(ProjectModel.deprecated_at.is_(None))
             .filter(ProjectModel.version.isnot(None))
             .order_by(ProjectModel.id.desc())
             .first())
    return (model.version or "").strip() if model is not None else ""


def _record_key(row: TestItemRow) -> str:
    return f"{RECORD_PREFIX}{row.uuid or row.id}"


def _synthetic_record(row: TestItemRow):
    return (TestRunRecord.query
            .filter_by(project_id=row.project_id, task_key=_record_key(row))
            .first())


def write_untestable(project: Project, row: TestItemRow, *,
                     actor_id: Optional[int] = None) -> bool:
    """Land an approved exemption as an ``Untestable`` verdict. Caller commits.

    A 不要 case *is* an untestable case: nobody will ever run it, and the reason
    has been signed off. Leaving it as ``Not Tested`` forever meant it showed up
    in no chart at all and exported with an empty 結果 cell, so the one thing the
    matrix is for -- being the report -- did not hold for these rows.

    Two rules this must not break:

    * **Real evidence wins.** If the row already carries a verdict, the case was
      actually executed at some point; overwriting that with ``Untestable``
      would destroy the only record of a real run to satisfy a paperwork state.
    * **Go through the collab write-back.** Assigning ``row.result`` directly is
      reverted by the editor's next flush (that is the whole reason
      :mod:`app.collab.writeback` exists).

    Returns True when a verdict was written.
    """
    if rwb.classify(row.result or ""):
        logger.info("exemption: row %s already has verdict %r, not overwriting",
                    row.uuid, row.result)
        return False

    now = _utcnow()
    values: dict[str, Any] = {
        "result": rwb.UNTESTABLE,
        "exec_date": rwb.local_date(now),
    }
    # 実施者 is read by people. A verdict with no author is unanswerable later,
    # and the approver is precisely who is accountable for this one.
    who = db.session.get(LMUser, int(actor_id)) if actor_id else None
    if who is not None:
        values["executor"] = (who.display_name or who.username or "").strip()
    # Never blank a label a human typed by hand.
    version = (row.get_field("version_label") or "").strip() \
        or _current_model_version(project)
    if version:
        values["version_label"] = version

    writeback.apply_server_fields(project.id, row, values)

    # The charts read run records, not row columns -- without this the case is
    # invisible in the per-version breakdown no matter what the row says.
    if _synthetic_record(row) is None:
        db.session.add(TestRunRecord(
            project_id=project.id,
            row_uuid=row.uuid,
            test_id=sje.row_test_id(row),
            task_key=_record_key(row),
            verdict=rwb.UNTESTABLE,
            outcome="untestable",
            model_name="",
            model_version=version,
            executor_id=int(actor_id) if actor_id else None,
            executor_name=values.get("executor", ""),
            executed_at=now,
            executed_on=values["exec_date"],
        ))
    return True


def _retract_untestable(row: TestItemRow) -> None:
    """Undo :func:`write_untestable`. Caller commits.

    Only ever touches what the sign-off itself wrote: a real run's record has a
    different ``task_key`` and is left alone, because run history is evidence and
    evidence is not editable by changing your mind about scope.
    """
    record = _synthetic_record(row)
    if record is not None:
        db.session.delete(record)
    if (row.result or "").strip().lower() == rwb.UNTESTABLE.lower():
        writeback.apply_server_fields(row.project_id, row, {
            "result": "", "exec_date": "", "executor": "",
        })


# --------------------------------------------------------------------------- #
# Deciding
# --------------------------------------------------------------------------- #
def decide(project: Project, row: TestItemRow, approve: bool, *,
           actor_id: int, note: str = "") -> TestItemRow:
    """Approve or reject one pending exemption.

    A note is mandatory either way. Approving is a permanent reduction of the
    tested surface, so "why was this case never written?" must be answerable
    from the row itself years later; rejecting sends work back to someone who
    needs to know what to do instead.
    """
    if effective_status(row) != PENDING:
        raise ExemptionError("该用例当前不在待审批状态（項目作成 需为『不要』且尚未审批）。")

    note = (note or "").strip()
    if not note:
        raise ExemptionError("审批『不要』时必须填写理由。")

    row.exempt_status = APPROVED if approve else REJECTED
    row.exempt_value = NOT_REQUIRED
    row.exempt_note = note
    row.exempt_reviewer_id = int(actor_id)
    row.exempt_decided_at = _utcnow()
    row.version = (row.version or 1) + 1
    row.updated_at = _utcnow()

    # An approved 不要 is an Untestable case: give it the verdict so it appears
    # in the per-version and burn-up charts and exports with a filled 結果 cell.
    # This deliberately does NOT go through review_service.request_review -- the
    # project's policy asks for Untestable to be reviewed, and this sign-off IS
    # that review. Routing it again would demand two approvals of one fact.
    if approve:
        write_untestable(project, row, actor_id=actor_id)

    target = row.exempt_requested_by or row.updated_by or row.created_by
    if target:
        kind = (notify_svc.REVIEW_APPROVED if approve
                else notify_svc.REVIEW_REJECTED)
        verb = "已通过" if approve else "被驳回"
        label = row.get_field("test_id") or row.case_id or row.title or "case"
        notify_svc.notify(
            target, kind,
            f"『不要』审批{verb}：{label}",
            body=(f"驳回理由：{note}" if not approve
                  else f"已确认无需作成：{note}"),
            project_id=project.id,
            link_url=notify_svc.review_item_link(project.id, row.uuid or ""),
            ref_type="test_item", ref_id=row.uuid or str(row.id),
            group_key=f"exemption.{kind}:{project.id}:{row.uuid or row.id}",
            actor_id=actor_id,
        )
    return row


def decide_bulk(project: Project, rows: Iterable[TestItemRow], approve: bool, *,
                actor_id: int, note: str = "") -> dict:
    """Approve/reject many exemptions at once.

    Bulk is allowed here (unlike ``Untestable``) because 不要 is normally
    decided per feature area rather than per case -- but the note is still
    required, and it is recorded on every row so no approval is left without a
    justification.
    """
    done, skipped = [], []
    for row in rows:
        try:
            decide(project, row, approve, actor_id=actor_id, note=note)
            done.append(row.uuid)
        except ExemptionError as exc:
            skipped.append({"uuid": row.uuid, "reason": str(exc)})
    db.session.commit()
    return {"approved" if approve else "rejected": done, "skipped": skipped}


def reset(row: TestItemRow) -> None:
    """Drop a decision, putting the row back in the queue. Caller commits.

    Also retracts the ``Untestable`` verdict the approval wrote: leaving it
    behind would keep the case counted as executed in every chart while its
    exemption sat un-decided, which is the exact discrepancy the sign-off gate
    exists to prevent.
    """
    if row.exempt_status == APPROVED:
        _retract_untestable(row)
    row.exempt_status = NONE
    row.exempt_value = ""
    row.exempt_note = ""
    row.exempt_decided_at = None


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def _live_rows(project_ids: Iterable[int]):
    """Live test-sheet rows, the same filter every other counter uses."""
    pids = [int(p) for p in project_ids]
    return (TestItemRow.query
            .filter(TestItemRow.project_id.in_(pids))
            .filter(TestItemRow.sheet == "test")
            .filter(TestItemRow.deleted_at.is_(None)))


def _matches(row: TestItemRow, status: str) -> bool:
    state = effective_status(row)
    if not state:
        return False
    if status == STATUS_ALL:
        return True
    if status == STATUS_DECIDED:
        return state in DECIDED
    return state == status


def queue_for(project_ids: Iterable[int], *, status: str = PENDING,
              reviewer_id: Optional[int] = None,
              requester_id: Optional[int] = None,
              limit: int = 200) -> list[TestItemRow]:
    """Exemption rows across projects, newest activity first.

    Filtering happens in Python rather than SQL because "pending" is derived
    from a JSON field: pushing it into the query would mean hand-writing a
    JSON path expression per database backend, and getting it subtly wrong
    there means rows silently disappearing from a *sign-off queue*.

    The SQL side still narrows hard -- only rows that either claim ``不要`` or
    carry a decision can qualify, and both imply a non-null
    ``exempt_requested_at`` once :func:`sync_pending` has run, so the scan is
    bounded by the project's row count and nothing worse.
    """
    pids = [int(p) for p in project_ids]
    if not pids:
        return []
    status = status if status in STATUSES else PENDING
    cap = max(1, min(int(limit), 500))

    q = _live_rows(pids)
    if reviewer_id:
        q = q.filter(TestItemRow.exempt_reviewer_id == int(reviewer_id))
    if requester_id:
        # "Decisions on what I raised". A claim raised before sync_pending ran
        # has no requester recorded, so fall back to whoever last touched the
        # row -- otherwise the person who typed 不要 never sees it was rejected.
        uid = int(requester_id)
        q = q.filter(db.or_(
            TestItemRow.exempt_requested_by == uid,
            db.and_(TestItemRow.exempt_requested_by.is_(None),
                    db.or_(TestItemRow.updated_by == uid,
                           TestItemRow.created_by == uid))))
    activity = db.func.coalesce(TestItemRow.exempt_decided_at,
                                TestItemRow.exempt_requested_at)
    rows = (q.order_by(db.nullslast(activity.desc()), TestItemRow.id.desc())
             .limit(cap * 4)
             .all())
    return [r for r in rows if _matches(r, status)][:cap]


def counts_for(project_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """``{project_id: {pending|approved|rejected: count}}``."""
    pids = [int(p) for p in project_ids]
    out: dict[int, dict[str, int]] = {p: {PENDING: 0, APPROVED: 0, REJECTED: 0}
                                      for p in pids}
    if not pids:
        return out
    for row in _live_rows(pids).all():
        state = effective_status(row)
        if state:
            out.setdefault(row.project_id,
                           {PENDING: 0, APPROVED: 0, REJECTED: 0})
            out[row.project_id][state] += 1
    return out


def counts_by_reviewer(user_id: int, project_ids: Iterable[int]) -> dict:
    """``{pending, decided}`` for the claims routed to *this* user.

    The workspace badge and KPI tile must count the user's own backlog, not
    every project's. Showing the project-wide figure under a label that says
    待批 would tell a reviewer with nothing to do that dozens of claims are
    waiting on them -- and the number would never fall no matter how much they
    signed off.

    Derived in Python for the same reason as :func:`queue_for`: "pending" is a
    function of a JSON cell plus the stored decision, and a SQL approximation
    of it would drift from the list it is supposed to label.
    """
    out = {PENDING: 0, STATUS_DECIDED: 0}
    pids = [int(p) for p in project_ids]
    if not pids or not user_id:
        return out
    rows = (_live_rows(pids)
            .filter(TestItemRow.exempt_reviewer_id == int(user_id))
            .all())
    for row in rows:
        state = effective_status(row)
        if state == PENDING:
            out[PENDING] += 1
        elif state in DECIDED:
            out[STATUS_DECIDED] += 1
    return out


def _raised_by(row: TestItemRow, user_id: int) -> bool:
    """Did *user_id* raise this claim?

    Mirrors the fallback chain :func:`row_review_dict` and
    ``review_service.counts_by_role`` use: a claim written before
    ``exempt_requested_by`` existed has no requester recorded, so authorship
    stands in. Without the fallback the person who typed 不要 would never be
    counted as the one whose claim was rejected.
    """
    if row.exempt_requested_by:
        return int(row.exempt_requested_by) == int(user_id)
    return int(user_id) in {int(v) for v in (row.updated_by, row.created_by)
                            if v}


def counts_by_role(user_id: int, project_ids: Iterable[int]) -> dict:
    """``{pending, rejected, decided}`` for one user's exemption queue.

    The counterpart of ``review_service.counts_by_role``, and the reason it
    exists: the workspace tabs mix both kinds of sign-off into one list, so
    counting only verdict reviews puts a number on the 我被驳回 tab that is
    smaller than the list underneath it.

    ``pending`` and ``decided`` are the reviewer's own workload; ``rejected``
    is the *requester's* -- "claims I raised that came back" -- exactly as in
    the review queue, so the same tab means the same thing on both halves.
    """
    out = {PENDING: 0, REJECTED: 0, STATUS_DECIDED: 0}
    pids = [int(p) for p in project_ids]
    if not pids or not user_id:
        return out
    uid = int(user_id)
    for row in _live_rows(pids).all():
        state = effective_status(row)
        if not state:
            continue
        if row.exempt_reviewer_id and int(row.exempt_reviewer_id) == uid:
            if state == PENDING:
                out[PENDING] += 1
            elif state in DECIDED:
                out[STATUS_DECIDED] += 1
        if state == REJECTED and _raised_by(row, uid):
            out[REJECTED] += 1
    return out


def review_user_ids(rows: Iterable[TestItemRow]) -> set[int]:
    """Every user id named by these rows' exemptions, for one bulk lookup."""
    ids: set[int] = set()
    for row in rows:
        for value in (row.exempt_reviewer_id, row.exempt_requested_by):
            if value:
                ids.add(int(value))
    return ids


def _user_name(users: Optional[dict[int, LMUser]], user_id: Optional[int]) -> str:
    if not user_id or not users:
        return ""
    user = users.get(int(user_id))
    if not user:
        return ""
    return user.display_name or user.username or ""


def row_review_dict(row: TestItemRow,
                    users: Optional[dict[int, LMUser]] = None) -> dict:
    """An exemption projected into the *review* queue's shape.

    A 不要 claim and a runner's Untestable verdict are the same question asked
    of the same person -- "should this case count as work that will never be
    done?" -- so they belong in one queue. Splitting them into two tabs made the
    reviewer learn a distinction that exists only in our storage.

    The projection reports ``review_verdict`` as ``Untestable`` because that is
    what approving it will actually write onto the row, so the list shows the
    consequence rather than an internal state name. ``kind`` tells the client
    which endpoint decides it.
    """
    from . import review_routes

    requester = row.exempt_requested_by or row.updated_by or row.created_by
    return {
        "kind": "exemption",
        "uuid": row.uuid,
        "id": row.id,
        "project_id": row.project_id,
        "case_id": row.case_id,
        "test_id": sje.row_test_id(row),
        "title": row.title,
        "category": review_routes.row_category(row),
        "result": row.result,
        "review_status": effective_status(row),
        "review_verdict": rwb.UNTESTABLE,
        "review_note": row.exempt_note or "",
        "reviewer_id": row.exempt_reviewer_id,
        "reviewer_name": _user_name(users, row.exempt_reviewer_id),
        "requester_id": requester,
        "requester_name": _user_name(users, requester),
        "review_requested_at": (row.exempt_requested_at.isoformat()
                                if row.exempt_requested_at else None),
        "reviewed_at": (row.exempt_decided_at.isoformat()
                        if row.exempt_decided_at else None),
        "description": row.get_field("description") or "",
        "executor": row.get_field("executor") or "",
        "exec_date": row.get_field("exec_date") or "",
        "version_label": row.get_field("version_label") or "",
        # Mandatory in both directions, so it can never be bulk-approved: the
        # same rule the Untestable verdict already follows.
        "needs_note": True,
        "bulk_approvable": False,
    }


def row_dict(row: TestItemRow,
             users: Optional[dict[int, LMUser]] = None) -> dict:
    """Serialise one row's exemption for the queue UI.

    Carries enough of the case (id, title, 区分) to be judged from the list:
    a queue that shows only a uuid forces the reviewer to open every row to
    learn anything.
    """
    from . import review_routes

    return {
        "uuid": row.uuid,
        "project_id": row.project_id,
        "case_id": row.case_id,
        "test_id": row.get_field("test_id") or "",
        "title": row.title,
        "category": review_routes.row_category(row),
        "item_created": row_value(row),
        "status": effective_status(row),
        "note": row.exempt_note or "",
        "reviewer_id": row.exempt_reviewer_id,
        "reviewer_name": _user_name(users, row.exempt_reviewer_id),
        "requested_by": row.exempt_requested_by,
        "requester_name": _user_name(users, row.exempt_requested_by),
        "requested_at": (row.exempt_requested_at.isoformat()
                         if row.exempt_requested_at else None),
        "decided_at": (row.exempt_decided_at.isoformat()
                       if row.exempt_decided_at else None),
    }
