"""Reorder planning (items_service.plan_move).

``row_order`` is project-wide: _max_row_order() does not filter by sheet, so
rows from the test / const / lib sheets share one interleaved sequence. The old
move_items() swapped raw neighbours in that global sequence, which meant a row
could trade places with a row from a DIFFERENT sheet -- leaving the visible
order of both sheets unchanged while still rewriting row_order and writing an
``item.reorder`` audit entry. Phase 12 puts 上移/下移 on the toolbar as the only
reachable entry point under the Univer engine, so that silent no-op would have
become a prominent broken button.

plan_move() is deliberately free of database access so these rules can be
tested directly -- this suite runs without PostgreSQL.
"""
import os
import sys
import unittest


def _find_repo():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.dirname(here), here):
        if os.path.isdir(os.path.join(cand, "app", "services", "lanmatrix")):
            return cand
    raise AssertionError("repo root not found from " + here)


REPO = _find_repo()
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from app.services.lanmatrix.items_service import (  # noqa: E402
    MixedSheetMove, plan_move,
)


def apply(rows, changes):
    """Return (id, sheet, order) rows re-sorted after applying ``changes``."""
    out = [(rid, sheet, changes.get(rid, order)) for rid, sheet, order in rows]
    return sorted(out, key=lambda r: r[2])


def visible(rows, sheet):
    return [rid for rid, s, _o in rows if s == sheet]


class TestSheetScoping(unittest.TestCase):
    """The bug this function exists to fix."""

    # A const row parked between two test rows in the global sequence.
    ROWS = [(1, "test", 1), (2, "const", 2), (3, "test", 3)]

    def test_move_up_skips_over_a_foreign_sheet_row(self):
        changes = plan_move(self.ROWS, [3], "up")
        after = apply(self.ROWS, changes)
        self.assertEqual(visible(after, "test"), [3, 1],
                         "the test row must actually move past its own neighbour")

    def test_foreign_sheet_rows_keep_their_slots(self):
        changes = plan_move(self.ROWS, [3], "up")
        self.assertNotIn(2, changes, "a const row must not be renumbered by a test-sheet move")
        after = apply(self.ROWS, changes)
        self.assertEqual([r for r in after if r[0] == 2][0][2], 2,
                         "the const row keeps row_order 2")

    def test_only_slots_of_the_moving_sheet_are_reused(self):
        changes = plan_move(self.ROWS, [3], "up")
        self.assertEqual(set(changes.values()) - {1, 3}, set(),
                         "the test rows swap only the slots test rows already held")

    def test_move_down_is_symmetric(self):
        changes = plan_move(self.ROWS, [1], "down")
        after = apply(self.ROWS, changes)
        self.assertEqual(visible(after, "test"), [3, 1])
        self.assertNotIn(2, changes)

    def test_orders_stay_unique(self):
        changes = plan_move(self.ROWS, [3], "up")
        after = apply(self.ROWS, changes)
        orders = [o for _i, _s, o in after]
        self.assertEqual(len(orders), len(set(orders)), "row_order must stay unique")

    def test_the_sequence_stays_gap_free(self):
        changes = plan_move(self.ROWS, [3], "up")
        after = apply(self.ROWS, changes)
        self.assertEqual(sorted(o for _i, _s, o in after), [1, 2, 3])

    def test_mixed_sheet_selection_is_refused(self):
        with self.assertRaises(MixedSheetMove):
            plan_move(self.ROWS, [1, 2], "up")

    def test_unknown_ids_are_ignored(self):
        self.assertEqual(plan_move(self.ROWS, [999], "up"), {})


class TestBoundaries(unittest.TestCase):
    ROWS = [(1, "test", 1), (2, "test", 2), (3, "test", 3)]

    def test_top_row_cannot_move_up(self):
        self.assertEqual(plan_move(self.ROWS, [1], "up"), {})

    def test_bottom_row_cannot_move_down(self):
        self.assertEqual(plan_move(self.ROWS, [3], "down"), {})

    def test_whole_sheet_selected_is_a_no_op(self):
        self.assertEqual(plan_move(self.ROWS, [1, 2, 3], "up"), {})
        self.assertEqual(plan_move(self.ROWS, [1, 2, 3], "down"), {})

    def test_empty_selection_is_a_no_op(self):
        self.assertEqual(plan_move(self.ROWS, [], "up"), {})
        self.assertEqual(plan_move(self.ROWS, None, "up"), {})

    def test_single_row_sheet(self):
        self.assertEqual(plan_move([(1, "test", 1)], [1], "up"), {})
        self.assertEqual(plan_move([(1, "test", 1)], [1], "down"), {})

    def test_empty_project(self):
        self.assertEqual(plan_move([], [1], "up"), {})


class TestBlockMove(unittest.TestCase):
    """A multi-row selection slides as one unit and stays contiguous."""

    ROWS = [(i, "test", i) for i in range(1, 6)]

    def test_block_slides_up_as_a_unit(self):
        after = apply(self.ROWS, plan_move(self.ROWS, [3, 4], "up"))
        self.assertEqual(visible(after, "test"), [1, 3, 4, 2, 5])

    def test_block_slides_down_as_a_unit(self):
        after = apply(self.ROWS, plan_move(self.ROWS, [2, 3], "down"))
        self.assertEqual(visible(after, "test"), [1, 4, 2, 3, 5])

    def test_block_pinned_at_the_top_does_not_move(self):
        self.assertEqual(plan_move(self.ROWS, [1, 2], "up"), {})

    def test_block_pinned_at_the_bottom_does_not_move(self):
        self.assertEqual(plan_move(self.ROWS, [4, 5], "down"), {})

    def test_a_split_selection_keeps_both_parts_moving(self):
        after = apply(self.ROWS, plan_move(self.ROWS, [2, 4], "up"))
        self.assertEqual(visible(after, "test"), [2, 1, 4, 3, 5])

    def test_a_split_selection_at_the_top_moves_only_the_free_part(self):
        # Row 1 is already at the top; row 3 still has room.
        after = apply(self.ROWS, plan_move(self.ROWS, [1, 3], "up"))
        self.assertEqual(visible(after, "test"), [1, 3, 2, 4, 5])

    def test_selection_order_does_not_matter(self):
        a = plan_move(self.ROWS, [4, 3], "up")
        b = plan_move(self.ROWS, [3, 4], "up")
        self.assertEqual(a, b)

    def test_duplicate_ids_are_harmless(self):
        a = plan_move(self.ROWS, [3, 3, 3], "up")
        b = plan_move(self.ROWS, [3], "up")
        self.assertEqual(a, b)


class TestNonContiguousSlots(unittest.TestCase):
    """Slots need not be 1..N: other sheets' rows sit between them."""

    ROWS = [(1, "test", 2), (2, "lib", 5), (3, "test", 9), (4, "test", 11)]

    def test_moving_reuses_only_the_sheets_own_slots(self):
        changes = plan_move(self.ROWS, [3], "up")
        self.assertEqual(set(changes.values()), {2, 9},
                         "rows swap the sparse slots the test sheet already held")

    def test_the_lib_row_is_untouched(self):
        self.assertNotIn(2, plan_move(self.ROWS, [3], "up"))
        self.assertNotIn(2, plan_move(self.ROWS, [1], "down"))

    def test_visible_order_flips(self):
        after = apply(self.ROWS, plan_move(self.ROWS, [3], "up"))
        self.assertEqual(visible(after, "test"), [3, 1, 4])

    def test_no_slot_is_invented(self):
        before = {o for _i, s, o in self.ROWS if s == "test"}
        changes = plan_move(self.ROWS, [4], "up")
        self.assertTrue(set(changes.values()) <= before,
                        "a move must never allocate a new row_order")


class TestReturnShape(unittest.TestCase):
    ROWS = [(1, "test", 1), (2, "test", 2)]

    def test_only_changed_rows_are_returned(self):
        changes = plan_move(self.ROWS, [2], "up")
        self.assertEqual(sorted(changes), [1, 2])
        for rid, new in changes.items():
            old = [o for i, _s, o in self.ROWS if i == rid][0]
            self.assertNotEqual(old, new, "unchanged rows must not be reported")

    def test_bad_direction_is_rejected(self):
        for bad in ("UP", "left", "", None, 0):
            with self.assertRaises(ValueError):
                plan_move(self.ROWS, [1], bad)

    def test_mixed_sheet_error_is_a_value_error(self):
        # The route layer maps ValueError-family failures to a 400; keeping the
        # subclass relationship means a caller that forgets the specific type
        # still degrades to a validation error rather than a 500.
        self.assertTrue(issubclass(MixedSheetMove, ValueError))

    def test_string_ids_are_accepted(self):
        # ids arrive from JSON and may be strings.
        self.assertEqual(plan_move(self.ROWS, ["2"], "up"),
                         plan_move(self.ROWS, [2], "up"))


class TestServiceStillGuards(unittest.TestCase):
    """The DB wrapper must keep its own validation and audit behaviour."""

    def setUp(self):
        self.src = open(
            os.path.join(REPO, "app/services/lanmatrix/items_service.py"),
            "r", encoding="utf-8").read()
        marker = "def move_items("
        assert marker in self.src, "move_items was renamed; update this test"
        self.body = self.src[self.src.index(marker):]

    def test_move_items_delegates_to_plan_move(self):
        # `assertIn("plan_move(")` alone is not enough: a body that calls
        # plan_move and then throws the result away would still contain it.
        # The applied changes must BE the planner's output.
        self.assertRegex(
            self.body, r"changes\s*=\s*plan_move\(",
            "move_items must apply plan_move's result, not re-derive the order")
        self.assertNotRegex(
            self.body[:self.body.index("return len(sel)")],
            r"changes\s*=\s*(?!plan_move\()(dict\(|\{|\[)",
            "changes must have exactly one source of truth")

    def test_move_items_writes_back_every_planned_change(self):
        self.assertIn("r.row_order = changes[r.id]", self.body)

    def test_mixed_sheet_becomes_a_validation_error(self):
        self.assertIn("MixedSheetMove", self.body)
        self.assertIn("VALIDATION_ERROR", self.body)

    def test_audit_is_only_written_when_something_changed(self):
        rec = self.body.index("audit.record")
        guard = self.body.index("if changes:")
        self.assertLess(guard, rec,
                        "a no-op move must not write an item.reorder record")

    def test_direction_is_still_validated_at_the_service_boundary(self):
        self.assertIn('direction not in ("up", "down")', self.body)


if __name__ == "__main__":
    unittest.main()
