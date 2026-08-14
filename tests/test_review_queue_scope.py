"""The status filter shared by the two halves of the unified review queue.

``/api/v1/me/reviews`` merges two sources: verdict reviews (``review_service``)
and 項目作成=不要 exemption claims (``exemption_service``). The tab the user
clicks sets one ``status``, and both sources must honour it identically --
otherwise a tab shows rows that belong to a different tab.

The regression this pins down: the endpoint used to collapse every non-pending
status to ``decided`` for the exemption half. ``decided`` is approved+rejected,
so opening 我被驳回 (``role=requester&status=rejected``) listed exemptions that
had been *approved*. The client renders them with their real state, so an
approved-and-signed-off case appeared under "rejected" labelled 已通过.

Database-free on purpose: this is a pure mapping, and the rest of the suite
needs PostgreSQL. A guard that only runs when a database happens to be
available is not a guard.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.routes.lanmatrix import me as me_module
from app.services.lanmatrix import exemption_service, review_service

ME_SOURCE = pathlib.Path(inspect.getfile(me_module)).read_text(encoding="utf-8")


class TestStatusPassesThrough:
    @pytest.mark.parametrize("status", [review_service.PENDING,
                                        review_service.APPROVED,
                                        review_service.REJECTED,
                                        review_service.STATUS_DECIDED,
                                        review_service.STATUS_ALL])
    def test_every_review_status_survives_unchanged(self, status):
        # The two services spell the states identically, so translation is the
        # identity. Anything else means one half of the queue is answering a
        # different question than the other.
        assert me_module.exemption_status_for(status) == status

    def test_rejected_is_not_widened_to_decided(self):
        # The actual bug: 我被驳回 asked for rejected and got approved rows too.
        assert (me_module.exemption_status_for(review_service.REJECTED)
                != exemption_service.STATUS_DECIDED)
        assert (me_module.exemption_status_for(review_service.REJECTED)
                == exemption_service.REJECTED)

    def test_approved_is_not_widened_to_decided(self):
        assert (me_module.exemption_status_for(review_service.APPROVED)
                == exemption_service.APPROVED)

    def test_the_two_services_agree_on_the_vocabulary(self):
        # If one service ever gains a state the other lacks, pass-through stops
        # being safe and this test says so before a tab starts silently
        # dropping rows.
        assert set(review_service.STATUSES) == set(exemption_service.STATUSES)


class TestUnknownStatusIsSafe:
    @pytest.mark.parametrize("status", ["", "   ", None, "bogus", "DECIDED",
                                        "pending;drop", "all "])
    def test_unrecognised_values_fall_back_to_pending(self, status):
        # Falling back to pending never claims a decision that was not made;
        # falling back to decided (or all) would.
        got = me_module.exemption_status_for(status)
        if (status or "").strip() in exemption_service.STATUSES:
            pytest.skip("value is a legitimate status")
        assert got == exemption_service.PENDING

    def test_whitespace_around_a_real_status_is_tolerated(self):
        assert (me_module.exemption_status_for("  rejected  ")
                == exemption_service.REJECTED)


class TestEndpointUsesTheMapping:
    """Source-level guard: the fix must be wired in, not merely available."""

    def test_me_reviews_calls_the_helper(self):
        tree = ast.parse(ME_SOURCE)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "me_reviews")
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "exemption_status_for" in called

    def test_the_old_widening_is_gone(self):
        # The literal shape of the bug: STATUS_DECIDED chosen by an inline
        # conditional inside the endpoint.
        assert "exemption_service.STATUS_DECIDED" not in ME_SOURCE


class _Row:
    """Minimal stand-in for ``TestItemRow``'s authorship columns."""

    def __init__(self, requested_by=None, updated_by=None, created_by=None):
        self.exempt_requested_by = requested_by
        self.updated_by = updated_by
        self.created_by = created_by


class TestRaisedBy:
    """Who counts as having raised an exemption claim.

    The 我被驳回 badge is a count of "claims I raised that came back". If this
    predicate is narrower than the queue's own requester filter, the badge
    reads lower than the list beneath it -- which is the same class of defect
    as the status widening above, just expressed as a number.
    """

    def test_the_recorded_requester_wins(self):
        assert exemption_service._raised_by(_Row(requested_by=7), 7) is True
        assert exemption_service._raised_by(_Row(requested_by=7), 8) is False

    def test_a_recorded_requester_is_not_overridden_by_authorship(self):
        # Somebody else edited the row afterwards; the claim is still mine and
        # only mine.
        row = _Row(requested_by=7, updated_by=8, created_by=8)
        assert exemption_service._raised_by(row, 8) is False
        assert exemption_service._raised_by(row, 7) is True

    @pytest.mark.parametrize("row", [_Row(updated_by=5),
                                     _Row(created_by=5),
                                     _Row(updated_by=9, created_by=5)])
    def test_legacy_rows_fall_back_to_authorship(self, row):
        # Claims raised before exempt_requested_by existed. Without the
        # fallback the person who typed 不要 never learns it was rejected.
        assert exemption_service._raised_by(row, 5) is True

    def test_an_unattributable_row_belongs_to_nobody(self):
        assert exemption_service._raised_by(_Row(), 5) is False

    def test_the_fallback_matches_the_queue_filter(self):
        # queue_for's requester branch uses exactly this chain; drift between
        # them is what makes a badge and its list disagree.
        source = inspect.getsource(exemption_service.queue_for)
        for column in ("exempt_requested_by", "updated_by", "created_by"):
            assert column in source


class TestQueueCountsCoverBothHalves:
    def test_exemptions_report_every_tab(self):
        # Three tabs, three counters. Reporting only pending+decided leaves the
        # 我被驳回 badge counting verdict reviews alone.
        keys = set(exemption_service.counts_by_role(0, []))
        assert keys == {exemption_service.PENDING,
                        exemption_service.REJECTED,
                        exemption_service.STATUS_DECIDED}

    def test_the_endpoint_merges_all_three(self):
        assert "exemption_service.counts_by_role" in ME_SOURCE
        assert '("pending", "rejected", "decided")' in ME_SOURCE


class TestRoleIsNormalisedBeforeUse:
    """An unrecognised ``role`` must not unscope the exemption half.

    ``review_service.queue_for`` normalises internally, so a bogus role still
    yields *that* user's reviews. The exemption call takes explicit
    reviewer/requester ids, and a bogus role used to leave both None -- which
    asks for every user's claims in a personal queue.
    """

    def test_me_reviews_normalises_role_before_the_exemption_call(self):
        tree = ast.parse(ME_SOURCE)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "me_reviews")
        body = fn.body
        guard_at = next(
            (i for i, node in enumerate(body)
             if isinstance(node, ast.If)
             and "ROLES" in ast.dump(node.test)), None)
        assert guard_at is not None, "role is never normalised"
        call_at = next(
            (i for i, node in enumerate(body)
             if "exemption_service" in ast.dump(node)
             and "queue_for" in ast.dump(node)), None)
        assert call_at is not None
        assert guard_at < call_at, "role normalised after it was already used"

    def test_the_service_roles_are_the_source_of_truth(self):
        assert review_service.ROLES == (review_service.ROLE_REVIEWER,
                                        review_service.ROLE_REQUESTER)
