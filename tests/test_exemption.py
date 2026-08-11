"""Scope exemption for 項目作成 = 不要.

Database-free on purpose. The substance of this feature is a two-input state
machine (what the cell says, what was decided about it) and the arithmetic that
decides whether a case counts towards the plan. Both are pure, and both fail
silently when wrong -- a mis-classified row does not raise, it just quietly
leaves or re-enters the denominator, which is precisely the outcome the
approval gate exists to prevent.
"""

from __future__ import annotations

import pytest

from app.services.lanmatrix import exemption_service as es


class _Row:
    """Minimal stand-in for ``TestItemRow`` (only the accessors used here)."""

    def __init__(self, item_created=None, exempt_status="", exempt_value="",
                 case_id="C1", title="t", uuid="u1"):
        self._fields = {"item_created": item_created}
        self.exempt_status = exempt_status
        self.exempt_value = exempt_value
        self.case_id = case_id
        self.title = title
        self.uuid = uuid

    def get_field(self, key):
        return self._fields.get(key)


class TestNormaliseValue:
    @pytest.mark.parametrize("raw", ["不要", " 不要 ", "\u3000不要\u3000",
                                     "\t不要\n"])
    def test_spacing_does_not_change_the_claim(self, raw):
        # 項目作成 is typed by hand into a spreadsheet. If padding produced a
        # different value, the same claim would be pending in one row and
        # invisible in the next.
        assert es.normalise_value(raw) == es.NOT_REQUIRED

    @pytest.mark.parametrize("raw", ["作成完了", "作成中"])
    def test_other_values_survive(self, raw):
        assert es.normalise_value(raw) == raw

    @pytest.mark.parametrize("raw", [None, "", "   ", True, False, "-", "\u2014"])
    def test_blanks_and_placeholders_say_nothing(self, raw):
        assert es.normalise_value(raw) == ""

    def test_a_typo_is_never_coerced_into_a_claim(self):
        # Silently reading "不用" as "不要" would drop a case from the plan on
        # the strength of a slip of the keyboard.
        assert es.normalise_value("不用") != es.NOT_REQUIRED


class TestStateMachine:
    def test_a_bare_claim_is_pending(self):
        assert es.status_of("不要", "", "") == es.PENDING

    @pytest.mark.parametrize("value", ["作成完了", "作成中", "", None])
    def test_no_claim_means_no_exemption(self, value):
        assert es.status_of(value, "", "") == es.NONE

    @pytest.mark.parametrize("decision", [es.APPROVED, es.REJECTED])
    def test_a_decision_on_the_current_claim_stands(self, decision):
        assert es.status_of("不要", decision, "不要") == decision

    def test_a_decision_about_a_different_value_does_not_authorise_this_one(self):
        # The guard that stops an approval granted for one statement from being
        # reused as cover for another.
        assert es.status_of("不要", es.APPROVED, "作成中") == es.PENDING

    def test_a_decision_with_no_recorded_value_is_not_trusted(self):
        # Rows written before this column existed must re-enter the queue
        # rather than be assumed approved.
        assert es.status_of("不要", es.APPROVED, "") == es.PENDING

    def test_withdrawing_the_claim_makes_the_decision_dormant(self):
        # Editing the cell away from 不要 puts the case back in the plan even
        # though an approval is still on record.
        assert es.status_of("作成完了", es.APPROVED, "不要") == es.NONE

    def test_an_unknown_stored_status_is_not_an_approval(self):
        # Fail closed: garbage in the column must leave the row in the queue,
        # never out of the denominator.
        assert es.status_of("不要", "maybe", "不要") == es.PENDING

    def test_row_helpers_agree_with_the_pure_function(self):
        row = _Row(item_created="不要")
        assert es.is_claim(row) is True
        assert es.effective_status(row) == es.PENDING
        assert es.is_out_of_scope(row) is False

        row.exempt_status, row.exempt_value = es.APPROVED, "不要"
        assert es.effective_status(row) == es.APPROVED
        assert es.is_out_of_scope(row) is True

    def test_only_approval_takes_a_row_out_of_scope(self):
        for status, value in ((es.PENDING, ""), (es.REJECTED, "不要"), ("", "")):
            row = _Row(item_created="不要", exempt_status=status,
                       exempt_value=value)
            assert es.is_out_of_scope(row) is False

    def test_a_row_with_no_claim_is_never_out_of_scope(self):
        assert es.is_out_of_scope(_Row(item_created="作成中")) is False
        assert es.is_out_of_scope(None) is False


class TestDecideGuards:
    """The rules :func:`decide` enforces before touching a row."""

    def test_a_note_is_required_in_both_directions(self):
        # Approving permanently shrinks the tested surface and must stay
        # answerable later; rejecting has to tell someone what to do instead.
        for approve in (True, False):
            row = _Row(item_created="不要")
            with pytest.raises(es.ExemptionError):
                es.decide(_Project(), row, approve, actor_id=1, note="   ")
            assert row.exempt_status == ""

    def test_a_row_with_no_claim_cannot_be_decided(self):
        row = _Row(item_created="作成完了")
        with pytest.raises(es.ExemptionError):
            es.decide(_Project(), row, True, actor_id=1, note="ok")

    def test_a_decided_row_is_not_decided_twice(self):
        row = _Row(item_created="不要", exempt_status=es.APPROVED,
                   exempt_value="不要")
        with pytest.raises(es.ExemptionError):
            es.decide(_Project(), row, False, actor_id=1, note="changed my mind")


class _Project:
    id = 1
    owner_id = None
    review_routes = []
    default_reviewer_id = None
