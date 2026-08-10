"""Pure unit tests for the review vocabulary and its asymmetries.

These deliberately need no database: the rules they pin down (which verdicts are
reviewable, which may be bulk-approved, which demand a written note) are the
substance of the feature, and they must stay correct even when the DB-backed
integration tests cannot run.
"""

from __future__ import annotations

import pytest

from app.services.lanmatrix import review_service as rs


class TestPolicyKey:
    @pytest.mark.parametrize("verdict", ["PASS", "pass", "Passed", "OK", "success"])
    def test_pass_synonyms_map_to_one_bucket(self, verdict):
        # Runners spell success several ways; the review policy must not depend
        # on which one a given runner happened to emit.
        assert rs.policy_key(verdict) == "pass"

    @pytest.mark.parametrize("verdict", ["Untestable", "untestable", "UNTESTABLE"])
    def test_untestable_is_case_insensitive(self, verdict):
        assert rs.policy_key(verdict) == "untestable"

    @pytest.mark.parametrize("verdict", ["FAIL", "ERROR", "cancelled", "Not Tested"])
    def test_non_reviewable_verdicts(self, verdict):
        # A failure is already visible as a problem; review exists to scrutinise
        # claims that something is *fine*, not claims that it is broken.
        assert rs.policy_key(verdict) == ""

    @pytest.mark.parametrize("verdict", ["", None, "   "])
    def test_blank_is_not_reviewable(self, verdict):
        assert rs.policy_key(verdict) == ""

    def test_surrounding_whitespace_tolerated(self):
        assert rs.policy_key("  PASS  ") == "pass"


class TestBulkRules:
    def test_pass_is_bulk_approvable(self):
        # A regression sweep turns hundreds of cases green; one click each would
        # guarantee rubber-stamping.
        assert rs.is_bulk_approvable("PASS") is True

    def test_untestable_is_not_bulk_approvable(self):
        # Each untestable claim removes a case from the evidence base and is an
        # individual judgement call.
        assert rs.is_bulk_approvable("Untestable") is False

    def test_non_reviewable_is_not_bulk_approvable(self):
        assert rs.is_bulk_approvable("FAIL") is False
        assert rs.is_bulk_approvable("") is False


class TestNoteRules:
    def test_untestable_requires_note(self):
        # For an untestable case the justification *is* the evidence.
        assert rs.requires_note("Untestable") is True

    def test_pass_does_not_require_note(self):
        assert rs.requires_note("PASS") is False

    def test_unknown_verdict_requires_no_note(self):
        assert rs.requires_note("FAIL") is False

    def test_note_requirement_and_bulk_rule_are_complementary(self):
        # The two rules must never both apply: a verdict that needs an
        # individual written opinion cannot also be swept through in bulk.
        for verdict in ("PASS", "Untestable", "FAIL", ""):
            assert not (rs.requires_note(verdict)
                        and rs.is_bulk_approvable(verdict))


class TestStateConstants:
    def test_states_are_distinct(self):
        states = {rs.NONE, rs.PENDING, rs.APPROVED, rs.REJECTED}
        assert len(states) == 4

    def test_none_is_falsy_so_untouched_rows_read_as_no_review(self):
        # Existing rows default to "" in the schema; that must mean "no review
        # needed", not an unrecognised state.
        assert not rs.NONE

    def test_states_fit_the_column(self):
        # review_status is VARCHAR(16); a state that silently truncates would
        # corrupt the state machine.
        for s in (rs.PENDING, rs.APPROVED, rs.REJECTED):
            assert len(s) <= 16


class _FakeProject:
    """Minimal stand-in: resolve_reviewer only reads two attributes."""

    def __init__(self, default_reviewer_id=None, owner_id=None):
        self.default_reviewer_id = default_reviewer_id
        self.owner_id = owner_id


class _FakeRow:
    def __init__(self, reviewer_id=None):
        self.reviewer_id = reviewer_id


class TestResolveReviewer:
    """A review request must always have a recipient.

    Before the fallback chain existed the lookup stopped at ``row.reviewer_id``,
    which is only set by assigning a reviewer to that row by hand. Automatic
    requests therefore resolved to None: no notification, and a queue that
    filters by reviewer stayed permanently empty. Enabling review did nothing.
    """

    def test_explicit_wins_over_everything(self):
        p = _FakeProject(default_reviewer_id=2, owner_id=3)
        assert rs.resolve_reviewer(p, _FakeRow(reviewer_id=4), explicit=9) == 9

    def test_row_reviewer_beats_project_default(self):
        p = _FakeProject(default_reviewer_id=2, owner_id=3)
        assert rs.resolve_reviewer(p, _FakeRow(reviewer_id=4)) == 4

    def test_project_default_used_when_row_has_none(self):
        p = _FakeProject(default_reviewer_id=2, owner_id=3)
        assert rs.resolve_reviewer(p, _FakeRow()) == 2

    def test_falls_back_to_owner(self):
        # An unassigned review is invisible work. The owner is the one person
        # guaranteed to exist and to be able to reassign it.
        p = _FakeProject(owner_id=3)
        assert rs.resolve_reviewer(p, _FakeRow()) == 3

    def test_returns_none_only_when_project_has_no_owner(self):
        assert rs.resolve_reviewer(_FakeProject(), _FakeRow()) is None

    def test_row_is_optional(self):
        p = _FakeProject(default_reviewer_id=5, owner_id=3)
        assert rs.resolve_reviewer(p) == 5

    def test_zero_is_treated_as_unset(self):
        # A falsy id must not be mistaken for a real user and short-circuit the
        # chain into "assigned to nobody".
        p = _FakeProject(default_reviewer_id=0, owner_id=3)
        assert rs.resolve_reviewer(p, _FakeRow(reviewer_id=0), explicit=0) == 3
