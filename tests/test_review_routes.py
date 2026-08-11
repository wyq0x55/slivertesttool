"""Per-テスト区分 reviewer routing.

Database-free on purpose: matching a 区分 to a reviewer is the whole substance
of the feature, and a routing rule that silently never fires is the failure mode
that costs the most (a review request lands on the wrong person's desk, or on
nobody's, and nothing in the product says so).
"""

from __future__ import annotations

import pytest

from app.services.lanmatrix import review_routes as rr
from app.services.lanmatrix import review_service as rs


class TestNormaliseCategory:
    @pytest.mark.parametrize("raw", [1, "1", " 1 ", "01", 1.0, "1.0"])
    def test_numeric_spellings_collapse(self, raw):
        # Excel hands the same 区分 over as an int, a float or a zero-padded
        # string depending on the cell format. A rule typed as "1" must cover
        # all of them, or it looks broken for reasons invisible in the UI.
        assert rr.normalise_category(raw) == "1"

    def test_text_categories_are_kept_verbatim(self):
        assert rr.normalise_category("  ECU-A ") == "ECU-A"

    @pytest.mark.parametrize("raw", [None, "", "   ", True, False])
    def test_blank_and_bool_are_not_categories(self, raw):
        # bool is an int subclass; letting True become "1" would route rows by
        # an accident of typing.
        assert rr.normalise_category(raw) == ""

    def test_non_integer_float_is_preserved(self):
        assert rr.normalise_category("1.5") == "1.5"


class TestMatching:
    def test_exact_match(self):
        assert rr.matches("5", "5") is True
        assert rr.matches("5", "6") is False

    def test_exact_match_normalises_both_sides(self):
        assert rr.matches("05", "5") is True

    def test_trailing_wildcard_is_a_prefix_rule(self):
        assert rr.matches("1*", "1") is True
        assert rr.matches("1*", "10") is True
        assert rr.matches("1*", "19") is True
        assert rr.matches("1*", "2") is False

    def test_bare_star_matches_any_category(self):
        assert rr.matches("*", "7") is True

    def test_text_matching_is_case_insensitive(self):
        assert rr.matches("ecu*", "ECU-A") is True

    def test_blank_never_matches(self):
        # An empty pattern or an uncategorised row must fall through to the
        # default reviewer rather than be captured by whichever rule is first.
        assert rr.matches("", "5") is False
        assert rr.matches("5", "") is False


class TestNormaliseRoutes:
    def test_keeps_order(self):
        out = rr.normalise_routes([
            {"category": "2", "reviewer_id": 1},
            {"category": "1", "reviewer_id": 2},
        ])
        assert [r["category"] for r in out] == ["2", "1"]

    def test_drops_unusable_entries(self):
        out = rr.normalise_routes([
            {"category": "", "reviewer_id": 1},      # no category
            {"category": "3"},                        # no reviewer
            {"category": "4", "reviewer_id": 0},      # falsy reviewer
            {"category": "5", "reviewer_id": "abc"},  # not an id
            "nonsense",
            {"category": "6", "reviewer_id": "7"},    # numeric string is fine
        ])
        assert out == [{"category": "6", "reviewer_id": 7}]

    def test_duplicate_patterns_are_dropped(self):
        # The second rule could never fire; keeping it would make the list lie
        # about what happens.
        out = rr.normalise_routes([
            {"category": "1", "reviewer_id": 4},
            {"category": "01", "reviewer_id": 9},
        ])
        assert out == [{"category": "1", "reviewer_id": 4}]

    def test_wildcard_stem_is_normalised(self):
        out = rr.normalise_routes([{"category": " 01* ", "reviewer_id": 3}])
        assert out == [{"category": "1*", "reviewer_id": 3}]

    def test_cap_is_enforced(self):
        raw = [{"category": str(i), "reviewer_id": 1}
               for i in range(rr.MAX_ROUTES + 25)]
        assert len(rr.normalise_routes(raw)) == rr.MAX_ROUTES

    @pytest.mark.parametrize("raw", [None, "", 5, {"category": "1"}])
    def test_non_list_input_is_empty(self, raw):
        assert rr.normalise_routes(raw) == []


class TestMatchReviewer:
    ROUTES = [
        {"category": "5", "reviewer_id": 7},
        {"category": "1*", "reviewer_id": 3},
    ]

    def test_first_match_wins(self):
        # Order is the only expression of precedence, and it is visible in the
        # UI. "5" is listed first, so it beats a later rule that also covers it.
        routes = [{"category": "5", "reviewer_id": 7},
                  {"category": "*", "reviewer_id": 99}]
        assert rr.match_reviewer(routes, "5") == 7

    def test_wildcard_rule_applies(self):
        assert rr.match_reviewer(self.ROUTES, "12") == 3

    def test_unmatched_category_returns_none(self):
        assert rr.match_reviewer(self.ROUTES, "9") is None

    def test_blank_category_returns_none(self):
        assert rr.match_reviewer(self.ROUTES, "") is None

    def test_empty_routes_return_none(self):
        assert rr.match_reviewer([], "5") is None


class _Row:
    """Row stand-in: テスト区分 lives in custom_values, read via get_field."""

    def __init__(self, category=None, reviewer_id=None):
        self._values = {rr.CATEGORY_KEY: category}
        self.reviewer_id = reviewer_id

    def get_field(self, key):
        return self._values.get(key)


class _Project:
    def __init__(self, routes=None, default_reviewer_id=None, owner_id=None):
        self.review_routes = routes or []
        self.default_reviewer_id = default_reviewer_id
        self.owner_id = owner_id


class TestRowCategory:
    def test_reads_and_normalises_the_field(self):
        assert rr.row_category(_Row(category=" 07 ")) == "7"

    def test_dict_rows_work_too(self):
        assert rr.row_category({rr.CATEGORY_KEY: 3}) == "3"

    def test_none_row_is_blank(self):
        assert rr.row_category(None) == ""


class TestResolveReviewerWithRoutes:
    """The chain: explicit -> row -> 区分 rule -> project default -> owner."""

    ROUTES = [{"category": "5", "reviewer_id": 7},
              {"category": "1*", "reviewer_id": 3}]

    def test_route_beats_project_default(self):
        # The whole point: 区分 5 goes to its own owner, not to the one person
        # the project used to send everything to.
        p = _Project(self.ROUTES, default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p, _Row(category="5")) == 7

    def test_wildcard_route_used(self):
        p = _Project(self.ROUTES, default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p, _Row(category="14")) == 3

    def test_unmatched_category_falls_back_to_default(self):
        p = _Project(self.ROUTES, default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p, _Row(category="9")) == 2

    def test_uncategorised_row_falls_back_to_default(self):
        p = _Project(self.ROUTES, default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p, _Row()) == 2

    def test_falls_back_to_owner_when_no_default(self):
        p = _Project(self.ROUTES, owner_id=1)
        assert rs.resolve_reviewer(p, _Row(category="9")) == 1

    def test_hand_assigned_row_reviewer_beats_the_rule(self):
        # Somebody deliberately named a reviewer for this case; a config change
        # must not quietly take it away from them.
        p = _Project(self.ROUTES, default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p, _Row(category="5", reviewer_id=8)) == 8

    def test_explicit_beats_the_rule(self):
        p = _Project(self.ROUTES, default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p, _Row(category="5"), explicit=9) == 9

    def test_project_without_routes_behaves_as_before(self):
        p = _Project([], default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p, _Row(category="5")) == 2

    def test_row_omitted_skips_routing(self):
        # Without a row there is no 区分 to route by; the default still applies.
        p = _Project(self.ROUTES, default_reviewer_id=2, owner_id=1)
        assert rs.resolve_reviewer(p) == 2
