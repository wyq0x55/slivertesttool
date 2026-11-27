"""Project dashboard aggregates.

What this answers
-----------------
"How far along is this project, and is the progress real?" Three questions the
raw matrix cannot answer at a glance:

1. **Coverage** -- of everything in scope, how much has actually been run?
2. **Momentum** -- is the executed count still climbing, or has it stalled?
3. **Trust** -- how much of the "done" pile is signed off, and how much is an
   unreviewed ``Untestable`` claim quietly counted as progress?

Question 3 is the reason the review funnel sits next to the progress ring
instead of on its own page. A burn-up chart that counts unreviewed
``Untestable`` cases as completed work reports a project as finished when part
of it was merely declared untestable. Keeping the two side by side makes that
gap visible rather than flattering.

Counting rules
--------------
Every case falls into exactly one bucket at each level, so the numbers add up:

    total  = out_of_scope + planned
    planned = not_run + executed
    executed = pass + fail + error + untestable

*Scope*: a case is out of scope when its result is blank **and** either

* it has been archived (``workflow_status`` = ``Archived``), or
* its 項目作成 says ``不要`` **and a reviewer has approved that claim**
  (:mod:`exemption_service`).

Both are deliberate, recorded acts by someone with the authority to perform
them. The approval requirement on the second is the whole point: a bare ``不要``
cell is a *proposal* to drop the case, and honouring it unreviewed would let
anyone improve the project's percentage by typing two characters. An unapproved
claim therefore stays in ``not_run`` and is reported separately as
``exempt_pending``, so the gap is visible rather than flattering.

*Executed* is derived from the row's current verdict, not from run history: a
case re-run from FAIL to PASS must count once, as a pass.

Trend data comes from ``lm_test_run_records`` (one row per case per run), which
records the model version and the local execution date at the time of the run.
History is therefore immutable -- renaming or deleting a model cannot rewrite a
past data point.

Everything here is read-only.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import case, literal

from ...extensions import db
from ...models import (
    DashboardDailyRollup,
    DashboardVersionRollup,
    LMUser,
    Project,
    TestBook,
    TestItemRow,
    TestRunRecord,
)
from . import settings
from . import exemption_service, review_service
from .comments_service import csv_cell
from .run_writeback_service import classify

logger = logging.getLogger(__name__)

#: Execution outcome buckets, in the order they are presented.
OUTCOMES = ("pass", "fail", "error", "untestable", "cancelled")

#: Outcomes that mean the case was actually exercised. ``cancelled`` is absent
#: on purpose: a cancelled run produced no evidence, so counting it as executed
#: would inflate progress with work that did not happen.
EXECUTED_OUTCOMES = ("pass", "fail", "error", "untestable")

#: Workflow status that takes a case out of scope.
ARCHIVED = "Archived"

#: Cap on trend points returned in one response. A project with years of history
#: should not be able to make this endpoint serialise an unbounded payload.
MAX_TREND_POINTS = 400

#: Cap on distinct versions charted. Beyond this the tail is folded into an
#: "其他" bucket rather than rendering an unreadable axis.
MAX_VERSIONS = 12


def _base_rows(project_id: int):
    """Live test-sheet rows for a project.

    Mirrors the filters ``review_service.counts_for`` uses -- test sheet only,
    not soft-deleted. The two must agree: a review funnel counted over a
    different row set than the progress ring is a dashboard that contradicts
    itself.
    """
    return TestItemRow.query.filter(
        TestItemRow.project_id == project_id,
        TestItemRow.sheet == "test",
        TestItemRow.deleted_at.is_(None),
    )


def summary(project_id: int) -> dict:
    """Coverage counters for one project.

    Returns a flat dict of integers plus the derived percentages, so the caller
    can render numbers even when the chart bundle is unavailable.
    """
    counts = {k: 0 for k in OUTCOMES}
    total = 0
    archived = 0
    not_run = 0
    exempt = {exemption_service.PENDING: 0,
              exemption_service.APPROVED: 0,
              exemption_service.REJECTED: 0}
    # Pending claims that are *also* still unexecuted. ``exempt_pending`` counts
    # every claim awaiting sign-off, including rows that carry a verdict anyway;
    # only this subset lives inside ``not_run``, so only this subset may be
    # carved out of it without making the chart's slices overlap.
    exempt_pending_not_run = 0

    # ``custom_values`` carries 項目作成; projecting the JSON column keeps this
    # to one query instead of hydrating every row just to read one key.
    rows = _base_rows(project_id).with_entities(
        TestItemRow.result, TestItemRow.workflow_status,
        TestItemRow.custom_values, TestItemRow.exempt_status,
        TestItemRow.exempt_value,
    )
    for result, workflow_status, custom, ex_status, ex_value in rows:
        total += 1
        item_created = (custom or {}).get(exemption_service.FIELD_KEY)
        ex_state = exemption_service.status_of(item_created, ex_status, ex_value)
        if ex_state:
            exempt[ex_state] += 1

        bucket = classify(result or "")
        if bucket in counts:
            # A case that actually ran is evidence, whatever the 項目作成 cell
            # claims. Counting it as out of scope would discard a real result.
            counts[bucket] += 1
            continue
        # No recognised verdict: either deliberately excluded, or simply not
        # run yet. Only an explicit archive, or an *approved* 不要 claim, takes
        # it out of the denominator -- a claim still awaiting sign-off stays in
        # ``not_run`` so nobody can leave the plan unilaterally.
        if (workflow_status or "").strip() == ARCHIVED:
            archived += 1
        elif ex_state == exemption_service.APPROVED:
            pass  # already counted in ``exempt``; folded into out_of_scope below
        else:
            not_run += 1
            if ex_state == exemption_service.PENDING:
                exempt_pending_not_run += 1

    executed = sum(counts[k] for k in EXECUTED_OUTCOMES)
    # A cancelled run leaves the case still owing a result, so it belongs with
    # the outstanding work rather than in its own dead-end bucket.
    not_run += counts["cancelled"]
    # An approved-but-already-executed row is counted under its verdict, not
    # here, so the exclusion is exactly the rows the loop skipped.
    exempt_out = total - archived - not_run - executed
    out_of_scope = archived + exempt_out
    planned = total - out_of_scope

    return {
        "total": total,
        "out_of_scope": out_of_scope,
        "archived": archived,
        # Approved 不要 rows that are genuinely out of the plan. Distinct from
        # ``exempt_approved``, which also counts rows whose claim was approved
        # but which carry a verdict anyway (those stay in the executed pile).
        #
        # In practice this is now ~0 and kept only as a leak detector. Approving
        # a claim writes an Untestable verdict onto the row
        # (exemption_service.write_untestable), and ``untestable`` is in
        # EXECUTED_OUTCOMES, so an approved row lands in ``executed`` rather than
        # here. A non-zero value therefore means a row was approved but never got
        # its verdict written back -- worth seeing, not worth a KPI tile. The
        # dashboard tile reads ``exempt_approved`` instead.
        "exempt_out_of_scope": exempt_out,
        "exempt_pending": exempt[exemption_service.PENDING],
        # The slice of ``not_run`` that is awaiting a 不要 decision, so a chart
        # can carve it out without double-counting a claimed row that already
        # has a verdict.
        "exempt_pending_not_run": exempt_pending_not_run,
        "exempt_approved": exempt[exemption_service.APPROVED],
        "exempt_rejected": exempt[exemption_service.REJECTED],
        "planned": planned,
        "not_run": not_run,
        "executed": executed,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "errored": counts["error"],
        "untestable": counts["untestable"],
        "cancelled": counts["cancelled"],
        "executed_pct": _pct(executed, planned),
        "passed_pct": _pct(counts["pass"], planned),
    }


def _pct(part: int, whole: int) -> float:
    """Percentage rounded to one decimal; 0.0 when the denominator is empty."""
    if not whole:
        return 0.0
    return round(part * 100.0 / whole, 1)


def review_summary(project_id: int) -> dict:
    """Review funnel for one project.

    ``needs_review`` counts cases whose verdict the project's policy subjects to
    review, which is why it can exceed pending + approved + rejected: rows that
    qualify but have not yet been routed are the interesting gap.
    """
    counts = review_service.counts_for([project_id]).get(project_id, {}) or {}
    pending = int(counts.get(review_service.PENDING, 0))
    approved = int(counts.get(review_service.APPROVED, 0))
    rejected = int(counts.get(review_service.REJECTED, 0))
    decided = approved + rejected
    return {
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "decided": decided,
        "total": pending + decided,
        "approved_pct": _pct(approved, pending + decided),
    }


def trend(project_id: int, *, limit: int = MAX_TREND_POINTS) -> dict:
    """Cumulative executed-case count per local calendar date.

    Counts **distinct cases**, not runs: re-running the same case ten times is
    not ten cases' worth of progress. A case is attributed to the first date it
    produced an executed outcome, so the line is monotonic and reads as
    "how much of the matrix has ever been covered".
    """
    day_counts = (
        db.session.query(
            DashboardDailyRollup.executed_on,
            DashboardDailyRollup.first_case_count,
        )
        .filter(
            DashboardDailyRollup.project_id == project_id,
            DashboardDailyRollup.first_case_count > 0,
        )
        .order_by(DashboardDailyRollup.executed_on.asc())
        .all()
    )

    per_day: dict[str, int] = {day: int(cnt) for day, cnt in day_counts}

    dates = sorted(per_day)
    if len(dates) > limit:
        dropped = dates[: len(dates) - limit]
        carried = sum(per_day[day] for day in dropped)
        dates = dates[len(dates) - limit :]
    else:
        carried = 0

    cumulative: list[int] = []
    daily: list[int] = []
    running = carried
    for day in dates:
        running += per_day[day]
        cumulative.append(running)
        daily.append(per_day[day])

    return {
        "dates": dates,
        "daily": daily,
        "cumulative": cumulative,
        "baseline": carried,
    }


def by_version(project_id: int, *, limit: int = MAX_VERSIONS) -> dict:
    """Outcome breakdown per model version.

    Uses the **latest** run of each case on each version, so re-running a failing
    case until it passes shows one result per case per version instead of
    counting every attempt. Versions are ordered by first appearance, which
    keeps a release timeline readable without assuming a version-number format
    the project may not follow.
    """
    rollup_rows = (
        db.session.query(DashboardVersionRollup)
        .filter(
            DashboardVersionRollup.project_id == project_id,
        )
        .order_by(
            DashboardVersionRollup.first_seen_at.asc(),
            DashboardVersionRollup.first_seen_id.asc(),
        )
        .all()
    )
    if not rollup_rows:
        return {
            "versions": [],
            "series": {outcome: [] for outcome in OUTCOMES},
            "folded": 0,
        }

    versions = [row.version_label for row in rollup_rows]
    tally: dict[str, dict[str, int]] = {
        row.version_label: {
            "pass": int(row.pass_count or 0),
            "fail": int(row.fail_count or 0),
            "error": int(row.error_count or 0),
            "untestable": int(row.untestable_count or 0),
            "cancelled": int(row.cancelled_count or 0),
        }
        for row in rollup_rows
    }

    folded: list[str] = []
    if len(versions) > limit:
        folded = versions[: len(versions) - limit]
        versions = versions[len(versions) - limit :]
        merged = {outcome: 0 for outcome in OUTCOMES}
        for label in folded:
            for outcome in OUTCOMES:
                merged[outcome] += tally[label][outcome]
        tally["其他"] = merged
        versions = ["其他"] + versions

    return {
        "versions": versions,
        "series": {
            outcome: [tally[ver_label][outcome] for ver_label in versions]
            for outcome in OUTCOMES
        },
        "folded": len(folded),
    }


def _case_key_expr():
    """SQL expression matching ``row_uuid or f'tid:{test_id}'``."""
    return case(
        (
            db.and_(
                TestRunRecord.row_uuid.isnot(None),
                TestRunRecord.row_uuid != "",
            ),
            TestRunRecord.row_uuid,
        ),
        else_=literal("tid:") + db.func.coalesce(TestRunRecord.test_id, ""),
    )


#: Left-to-right classes. ``other`` is any paired difference that is not
#: pass to fail/error, or fail/error to pass.
COMPARE_CHANGES = (
    "only_left",
    "only_right",
    "unchanged",
    "pass_to_fail",
    "fail_to_pass",
    "other",
)

_FAIL_OUTCOMES = frozenset({"fail", "error"})


def _parse_compare_ref(ref: str) -> tuple[str, str]:
    """Split ``name@version``. A bare name keeps an empty version.

    The last ``@`` is the separator, so a model name that contains ``@`` still
    resolves to one identity.
    """
    from ..project_model_service import parse_ref
    from .errors import ServiceError

    name, version = parse_ref(ref)
    if not name:
        raise ServiceError(
            "参数必须是 name@version，没有版本时只填模型名",
            code="VALIDATION_ERROR",
        )
    return name, version


def _latest_non_cancelled(project_id: int, model_name: str,
                          model_version: str, *, by_book: bool = False,
                          ) -> dict[str, TestRunRecord]:
    """Latest non-cancelled run of each case for one model identity.

    The identity is ``model_name`` plus ``model_version``. Sharing a version
    label does not merge two models. A cancelled row is not a result: an older
    non-cancelled run is kept, and a test id with only cancelled runs is absent.

    With ``by_book`` the case identity is ``(book_id, test_id)``, so a multi-book
    project never folds the same テストID from two specification books into one
    result. Without it the identity is the test id alone, which is the behaviour
    every single-book project relies on. Keys are opaque to callers; only
    :func:`compare_versions` consumes them.
    """
    test_id_key = db.func.trim(TestRunRecord.test_id)
    book_key = db.func.coalesce(
        db.cast(TestRunRecord.book_id, db.String), "")
    partition = [book_key, test_id_key] if by_book else test_id_key
    normalized_outcome = db.func.lower(db.func.trim(TestRunRecord.outcome))
    ranked = (
        db.session.query(
            TestRunRecord.id.label("record_id"),
            db.func.row_number().over(
                partition_by=partition,
                order_by=(TestRunRecord.executed_at.desc(),
                          TestRunRecord.id.desc()),
            ).label("run_rank"),
        )
        .filter(
            TestRunRecord.project_id == project_id,
            TestRunRecord.model_name == model_name,
            TestRunRecord.model_version == model_version,
            test_id_key != "",
            normalized_outcome.notin_(("", "cancelled")),
        )
        .subquery()
    )
    rows = (
        TestRunRecord.query.join(
            ranked, TestRunRecord.id == ranked.c.record_id)
        .filter(ranked.c.run_rank == 1)
        .all()
    )
    chosen: dict[str, TestRunRecord] = {}
    for row in rows:
        test_id = (row.test_id or "").strip()
        if not test_id:
            continue
        outcome = (row.outcome or "").strip().lower()
        if not outcome or outcome == "cancelled":
            continue
        case_key = f"{row.book_id or ''}|{test_id}" if by_book else test_id
        if case_key in chosen:
            continue
        chosen[case_key] = row
    return chosen


def _compare_change(left_outcome: Optional[str],
                    right_outcome: Optional[str]) -> str:
    """Classify one test id. Direction is left to right."""
    if left_outcome is None:
        return "only_right"
    if right_outcome is None:
        return "only_left"
    if left_outcome == right_outcome:
        return "unchanged"
    if left_outcome == "pass" and right_outcome in _FAIL_OUTCOMES:
        return "pass_to_fail"
    if left_outcome in _FAIL_OUTCOMES and right_outcome == "pass":
        return "fail_to_pass"
    return "other"


def _compare_outcome(row: Optional[TestRunRecord]) -> Optional[str]:
    if row is None:
        return None
    outcome = (row.outcome or "").strip().lower()
    return outcome or None


def _compare_row_uuid(left: Optional[TestRunRecord],
                      right: Optional[TestRunRecord]) -> str:
    """Matrix row locator. Prefer the right-hand uuid when it exists."""
    for record in (right, left):
        if record is None:
            continue
        token = (record.row_uuid or "").strip()
        if token:
            return token
    return ""


def compare_versions(project_id: int, left: str, right: str, *,
                     page: int = 1, page_size: int = 100) -> dict:
    """Compare the latest non-cancelled result of each test id.

    ``left`` and ``right`` are ``name@version`` references. A model with no
    version is the bare name. ``summary`` counts every test id; ``page`` only
    slices ``items``.
    """
    left_ref = (left or "").strip()
    right_ref = (right or "").strip()
    left_name, left_version = _parse_compare_ref(left_ref)
    right_name, right_version = _parse_compare_ref(right_ref)
    page = 1 if page < 1 else int(page)
    page_size = 1 if page_size < 1 else int(page_size)

    project = db.session.get(Project, project_id)
    by_book = bool(project and project.project_type == "silver_books")
    left_rows = _latest_non_cancelled(
        project_id, left_name, left_version, by_book=by_book)
    right_rows = _latest_non_cancelled(
        project_id, right_name, right_version, by_book=by_book)

    summary = {key: 0 for key in COMPARE_CHANGES}
    items = []
    case_keys = set(left_rows) | set(right_rows)

    def case_sort_key(case_key: str):
        row = left_rows.get(case_key) or right_rows.get(case_key)
        test_id = (row.test_id or "").strip() if row is not None else ""
        if by_book:
            return (test_id, int(row.book_id or 0) if row is not None else 0,
                    case_key)
        return (test_id, case_key)

    case_keys = sorted(case_keys, key=case_sort_key)
    book_names: dict[int, str] = {}
    if by_book:
        book_ids = {
            row.book_id for row in list(left_rows.values()) + list(right_rows.values())
            if row.book_id
        }
        if book_ids:
            book_names = {
                book.id: book.name
                for book in TestBook.query.filter(
                    TestBook.project_id == project_id,
                    TestBook.id.in_(book_ids),
                ).all()
            }
    start = (page - 1) * page_size
    stop = start + page_size
    for index, case_key in enumerate(case_keys):
        left_row = left_rows.get(case_key)
        right_row = right_rows.get(case_key)
        left_outcome = _compare_outcome(left_row)
        right_outcome = _compare_outcome(right_row)
        change = _compare_change(left_outcome, right_outcome)
        summary[change] += 1
        if start <= index < stop:
            row = right_row or left_row
            item = {
                "test_id": (row.test_id or "").strip() if row is not None else "",
                "left_outcome": left_outcome,
                "right_outcome": right_outcome,
                "change": change,
                "row_uuid": _compare_row_uuid(left_row, right_row),
            }
            if by_book:
                book_id = row.book_id if row is not None else None
                item["book_id"] = book_id
                item["book_name"] = book_names.get(book_id, "") if book_id else ""
            items.append(item)

    return {
        "left": left_ref,
        "right": right_ref,
        "summary": summary,
        "page": page,
        "page_size": page_size,
        "total": len(case_keys),
        "items": items,
    }


def test_run_history(project_id: int, test_id: str, *,
                     row_uuid: Optional[str] = None,
                     book_id: Optional[int] = None,
                     page: int = 1, page_size: int = 100) -> dict:
    """Return a bounded, newest-first page of matrix-backed run summaries."""
    test_id = (test_id or "").strip()
    row_uuid = (row_uuid or "").strip() or None
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), settings.PAGE_SIZE_MAX)
    query = TestRunRecord.query.filter(
        TestRunRecord.project_id == project_id,
        TestRunRecord.test_id == test_id,
    )
    if row_uuid:
        query = query.filter(TestRunRecord.row_uuid == row_uuid)
    elif book_id is not None:
        query = query.filter(TestRunRecord.book_id == book_id)
    book_name = ""
    if book_id is not None:
        book = TestBook.query.filter_by(
            id=book_id, project_id=project_id).first()
        book_name = book.name if book is not None else ""
    total = query.count()
    offset = (page - 1) * page_size
    rows = []
    if offset < total:
        rows = (
            query.order_by(TestRunRecord.executed_at.desc(), TestRunRecord.id.desc())
            .offset(offset)
            .limit(page_size)
            .all()
        )
    payload = {
        "test_id": test_id,
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [
            {
                "id": row.id,
                "row_uuid": row.row_uuid or "",
                "test_id": row.test_id,
                "verdict": row.verdict,
                "outcome": row.outcome,
                "model_name": row.model_name,
                "model_version": row.model_version,
                "executor_name": row.executor_name,
                "executed_at": (
                    row.executed_at.isoformat() if row.executed_at else None
                ),
                "executed_on": row.executed_on,
            }
            for row in rows
        ],
    }
    if book_id is not None:
        payload["book_id"] = book_id
        payload["book_name"] = book_name
    return payload


TEST_RUN_HISTORY_CSV_HEADER = (
    "project_code",
    "project_name",
    "test_id",
    "row_uuid",
    "verdict",
    "outcome",
    "model_name",
    "model_version",
    "executor_name",
    "executed_at",
    "executed_on",
)


def test_run_history_csv_rows(project: Project):
    """Yield every run record for a project in stable newest-first order."""
    by_book = project.project_type == "silver_books"
    header = TEST_RUN_HISTORY_CSV_HEADER + (("book_name",) if by_book else ())
    yield [csv_cell(value) for value in header]
    book_names = {}
    if by_book:
        book_names = dict(db.session.query(TestBook.id, TestBook.name)
                          .filter_by(project_id=project.id).all())
    query = (
        TestRunRecord.query.filter(TestRunRecord.project_id == project.id)
        .order_by(TestRunRecord.executed_at.desc(), TestRunRecord.id.desc())
        .yield_per(500)
    )
    for record in query:
        row = [
            csv_cell(project.code),
            csv_cell(project.name),
            csv_cell(record.test_id),
            csv_cell(record.row_uuid),
            csv_cell(record.verdict),
            csv_cell(record.outcome),
            csv_cell(record.model_name),
            csv_cell(record.model_version),
            csv_cell(record.executor_name),
            csv_cell(record.executed_at.isoformat() if record.executed_at else None),
            csv_cell(record.executed_on),
        ]
        if by_book:
            row.append(csv_cell(book_names.get(record.book_id)))
        yield row


def snapshot(project: Project) -> dict:
    """Everything the dashboard page needs, in one response.

    Bundled deliberately: four separate round trips would let the page render a
    progress ring and a review funnel computed moments apart, which is how a
    dashboard ends up quietly contradicting itself.
    """
    pid = project.id
    return {
        "project": {
            "id": pid,
            "code": getattr(project, "code", "") or "",
            "name": getattr(project, "name", "") or "",
        },
        "summary": summary(pid),
        "review": review_summary(pid),
        "trend": trend(pid),
        "by_version": by_version(pid),
        "review_policy": project.review_policy(),
        "default_reviewer_id": project.default_reviewer_id,
        # Resolved here rather than in the browser: the policy panel would
        # otherwise have to fetch the member list just to render one name.
        "default_reviewer_name": _reviewer_name(project.default_reviewer_id),
        # Per-テスト区分 routing, with names resolved for the same reason. A rule
        # pointing at a user who has since left the project must still render as
        # a name, otherwise the admin cannot tell which rule to fix.
        "review_routes": [
            {**rule, "reviewer_name": _reviewer_name(rule["reviewer_id"])}
            for rule in project.review_route_rules()
        ],
    }


def _reviewer_name(user_id: Optional[int]) -> str:
    if not user_id:
        return ""
    row = db.session.query(LMUser).filter(LMUser.id == user_id).first()
    if row is None:
        return ""
    return row.display_name or row.username or ""
