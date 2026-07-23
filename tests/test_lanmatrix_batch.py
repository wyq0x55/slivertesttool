"""Batch-operation engine tests (FR-BATCH-001)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from lm_helpers import load  # noqa: E402

lm = load()
batch = lm.batch


class ValidateOperationTest(unittest.TestCase):
    def test_unknown(self):
        with self.assertRaises(batch.BatchOperationError):
            batch.validate_operation({"op": "explode"})

    def test_set_requires_value(self):
        with self.assertRaises(batch.BatchOperationError):
            batch.validate_operation({"op": "set"})
        batch.validate_operation({"op": "set", "value": "x"})

    def test_prefix_requires_nonempty(self):
        with self.assertRaises(batch.BatchOperationError):
            batch.validate_operation({"op": "prefix", "value": ""})

    def test_increment_numeric(self):
        with self.assertRaises(batch.BatchOperationError):
            batch.validate_operation({"op": "increment", "value": "abc"})
        batch.validate_operation({"op": "increment", "value": 2})

    def test_multi_add_needs_list(self):
        with self.assertRaises(batch.BatchOperationError):
            batch.validate_operation({"op": "multi_add", "value": "x"})


class ApplyOperationTest(unittest.TestCase):
    def test_set_clear(self):
        self.assertEqual(batch.apply_operation({"op": "set", "value": "P"}, "x"), "P")
        self.assertIsNone(batch.apply_operation({"op": "clear"}, "x"))

    def test_prefix_suffix(self):
        self.assertEqual(batch.apply_operation({"op": "prefix", "value": "TC_"}, "1"), "TC_1")
        self.assertEqual(batch.apply_operation({"op": "suffix", "value": "!"}, "a"), "a!")
        self.assertEqual(batch.apply_operation({"op": "prefix", "value": "x"}, None), "x")

    def test_find_replace(self):
        self.assertEqual(
            batch.apply_operation({"op": "find_replace", "find": "a", "replace": "b"}, "aaa"),
            "bbb")

    def test_regex_replace(self):
        self.assertEqual(
            batch.apply_operation({"op": "regex_replace", "pattern": r"\d+", "replace": "#"}, "a12b3"),
            "a#b#")

    def test_increment_keeps_int(self):
        self.assertEqual(batch.apply_operation({"op": "increment", "value": 2}, 5), 7)
        self.assertIsInstance(batch.apply_operation({"op": "increment", "value": 2}, 5), int)
        self.assertAlmostEqual(batch.apply_operation({"op": "decrement", "value": 0.5}, 2.0), 1.5)

    def test_multi_add_remove(self):
        self.assertEqual(
            batch.apply_operation({"op": "multi_add", "value": ["b", "c"]}, ["a", "b"]),
            ["a", "b", "c"])
        self.assertEqual(
            batch.apply_operation({"op": "multi_remove", "value": ["a"]}, ["a", "b"]),
            ["b"])


if __name__ == "__main__":
    unittest.main()
