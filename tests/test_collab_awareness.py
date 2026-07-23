"""Pure (DB-free) tests for Awareness -> row-actor attribution.

Imports ``app.collab.awareness`` directly; it has no ``pycrdt`` / Flask / DB
dependency, so these run in the sandbox without a database.
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(__file__)
_MOD_PATH = os.path.join(_HERE, "..", "app", "collab", "awareness.py")
_spec = importlib.util.spec_from_file_location("lm_collab_awareness", _MOD_PATH)
awareness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(awareness)


class RowActorsTest(unittest.TestCase):
    def test_cursor_maps_uuid_to_user(self):
        states = {
            1: {"user": {"id": 7, "name": "a"}, "cursor": {"sheet": "test", "uuid": "u-1"}},
            2: {"user": {"id": 9, "name": "b"}, "cursor": {"sheet": "test", "uuid": "u-2"}},
        }
        self.assertEqual(awareness.row_actors(states), {"u-1": 7, "u-2": 9})

    def test_selection_used_when_no_cursor(self):
        states = {1: {"user": {"id": 3}, "selection": {"sheet": "lib", "uuid": "u-9"}}}
        self.assertEqual(awareness.row_actors(states), {"u-9": 3})

    def test_cursor_wins_over_selection(self):
        states = {1: {"user": {"id": 3},
                      "cursor": {"uuid": "u-cursor"},
                      "selection": {"uuid": "u-sel"}}}
        self.assertEqual(awareness.row_actors(states), {"u-cursor": 3})

    def test_highest_client_id_wins_on_same_row(self):
        # Two users focused on the same row: deterministic — ascending client_id
        # iteration lets the highest client_id win.
        states = {
            5: {"user": {"id": 50}, "cursor": {"uuid": "shared"}},
            2: {"user": {"id": 20}, "cursor": {"uuid": "shared"}},
        }
        self.assertEqual(awareness.row_actors(states), {"shared": 50})

    def test_missing_user_or_uuid_skipped(self):
        states = {
            1: {"cursor": {"uuid": "no-user"}},                 # no user.id
            2: {"user": {"id": 8}},                              # no cursor/selection
            3: {"user": {"id": 8}, "cursor": {"sheet": "test"}}, # cursor has no uuid
            4: {"user": {"id": 8}, "cursor": {"uuid": "   "}},   # blank uuid
        }
        self.assertEqual(awareness.row_actors(states), {})

    def test_non_int_user_id_coerced(self):
        states = {1: {"user": {"id": "42"}, "cursor": {"uuid": "u-1"}}}
        self.assertEqual(awareness.row_actors(states), {"u-1": 42})

    def test_bool_user_id_rejected(self):
        states = {1: {"user": {"id": True}, "cursor": {"uuid": "u-1"}}}
        self.assertEqual(awareness.row_actors(states), {})

    def test_bad_inputs_return_empty(self):
        self.assertEqual(awareness.row_actors(None), {})
        self.assertEqual(awareness.row_actors({}), {})
        self.assertEqual(awareness.row_actors({1: "notadict"}), {})


class SnapshotStatesTest(unittest.TestCase):
    def test_none_awareness(self):
        self.assertEqual(awareness.snapshot_states(None), {})

    def test_states_property(self):
        class A:
            states = {1: {"user": {"id": 1}}}
        snap = awareness.snapshot_states(A())
        self.assertEqual(snap, {1: {"user": {"id": 1}}})

    def test_states_property_is_copied(self):
        class A:
            def __init__(self):
                self.states = {1: {"user": {"id": 1}}}
        a = A()
        snap = awareness.snapshot_states(a)
        snap[2] = {"x": 1}
        self.assertNotIn(2, a.states)  # snapshot is a shallow copy

    def test_get_states_method_fallback(self):
        class A:
            states = None
            def get_states(self):
                return {9: {"user": {"id": 5}}}
        self.assertEqual(awareness.snapshot_states(A()), {9: {"user": {"id": 5}}})

    def test_raising_awareness_yields_empty(self):
        class A:
            @property
            def states(self):
                raise RuntimeError("boom")
        self.assertEqual(awareness.snapshot_states(A()), {})

    def test_non_dict_states_yields_empty(self):
        class A:
            states = ["not", "a", "dict"]
        self.assertEqual(awareness.snapshot_states(A()), {})

    def test_end_to_end_snapshot_then_actors(self):
        class A:
            states = {
                1: {"user": {"id": 11}, "cursor": {"uuid": "row-a"}},
                2: {"user": {"id": 22}, "selection": {"uuid": "row-b"}},
            }
        snap = awareness.snapshot_states(A())
        self.assertEqual(awareness.row_actors(snap), {"row-a": 11, "row-b": 22})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
