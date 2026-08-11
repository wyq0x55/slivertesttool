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
from collections import OrderedDict, defaultdict
from typing import Optional

from ...extensions import db
from ...models import LMUser, Project, TestItemRow, TestRunRecord
from . import exemption_service, review_service
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
    rows = (
        TestRunRecord.query.filter(
            TestRunRecord.project_id == project_id,
            TestRunRecord.outcome.in_(EXECUTED_OUTCOMES),
            TestRunRecord.executed_on != "",
        )
        .with_entities(
            TestRunRecord.executed_on,
            TestRunRecord.row_uuid,
            TestRunRecord.test_id,
        )
        .order_by(TestRunRecord.executed_on.asc())
        .all()
    )

    first_seen: dict[str, str] = {}
    for executed_on, row_uuid, test_id in rows:
        key = row_uuid or f"tid:{test_id}"
        if key not in first_seen:
            first_seen[key] = executed_on

    per_day: dict[str, int] = defaultdict(int)
    for day in first_seen.values():
        per_day[day] += 1

    dates = sorted(per_day)
    if len(dates) > limit:
        # Keep the most recent window; the cumulative baseline is carried in the
        # first retained point so the curve does not appear to restart at zero.
        dropped = dates[: len(dates) - limit]
        carried = sum(per_day[d] for d in dropped)
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
    rows = (
        TestRunRecord.query.filter(
            TestRunRecord.project_id == project_id,
            TestRunRecord.outcome.in_(OUTCOMES),
        )
        .with_entities(
            TestRunRecord.model_version,
            TestRunRecord.row_uuid,
            TestRunRecord.test_id,
            TestRunRecord.outcome,
            TestRunRecord.executed_at,
        )
        .order_by(TestRunRecord.executed_at.asc())
        .all()
    )

    order: "OrderedDict[str, None]" = OrderedDict()
    latest: dict[tuple[str, str], str] = {}
    for version, row_uuid, test_id, outcome, _executed_at in rows:
        label = (version or "").strip() or "(未标注)"
        order.setdefault(label, None)
        key = (label, row_uuid or f"tid:{test_id}")
        latest[key] = outcome  # ordered ascending, so the last write wins

    tally: dict[str, dict[str, int]] = defaultdict(lambda: {k: 0 for k in OUTCOMES})
    for (label, _case), outcome in latest.items():
        tally[label][outcome] += 1

    versions = list(order)
    folded: list[str] = []
    if len(versions) > limit:
        # Fold the oldest tail rather than truncating it: dropping data silently
        # would understate historical volume.
        folded = versions[: len(versions) - limit]
        versions = versions[len(versions) - limit :]
        merged = {k: 0 for k in OUTCOMES}
        for label in folded:
            for k in OUTCOMES:
                merged[k] += tally[label][k]
        tally["其他"] = merged
        versions = ["其他"] + versions

    return {
        "versions": versions,
        "series": {k: [tally[v][k] for v in versions] for k in OUTCOMES},
        "folded": len(folded),
    }


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
