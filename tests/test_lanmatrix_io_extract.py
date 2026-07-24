"""Pure I/O extraction (VHILS 手順 -> 入出力 pool) collection + dedupe tests.

Exercises ``libconst_bridge.collect_io_signals`` in isolation (Flask-free) via
the synthetic pure-package loader. The DB-facing ``extract_io_from_steps`` wraps
this collector and ``_run_io_import``; only the pure collection/dedupe contract
is unit-tested here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from lm_helpers import load  # noqa: E402

lm = load()
bridge = lm.libconst_bridge


class CollectIoSignalsTest(unittest.TestCase):
    def test_collects_inputs_and_expecteds(self):
        bodies = [{
            "input_signals": [["EngSpd", "ECU.Engine.Speed"]],
            "expected_signals": [["VehSpd", "ECU.Vehicle.Speed"]],
        }]
        res = bridge.collect_io_signals(bodies)
        self.assertEqual(res["scanned_rows"], 1)
        self.assertEqual(res["distinct_signals"], 2)
        self.assertEqual(res["items"], [
            {"io_name": "EngSpd", "io_path": "ECU.Engine.Speed"},
            {"io_name": "VehSpd", "io_path": "ECU.Vehicle.Speed"},
        ])

    def test_dedupes_case_insensitively_across_rows(self):
        bodies = [
            {"input_signals": [["Sig", "A.B"]]},
            {"input_signals": [["sig", "a.b"]]},   # dup (case-insensitive)
            {"expected_signals": [["Sig", "A.B"]]},  # dup again
        ]
        res = bridge.collect_io_signals(bodies)
        self.assertEqual(res["scanned_rows"], 3)
        self.assertEqual(res["distinct_signals"], 1)
        self.assertEqual(res["items"], [{"io_name": "Sig", "io_path": "A.B"}])

    def test_same_name_different_path_both_kept(self):
        bodies = [{"input_signals": [["Sig", "A.B"], ["Sig", "C.D"]]}]
        res = bridge.collect_io_signals(bodies)
        self.assertEqual(res["distinct_signals"], 2)
        self.assertEqual([i["io_path"] for i in res["items"]], ["A.B", "C.D"])

    def test_nameless_signal_skipped(self):
        bodies = [{"input_signals": [["", "A.B"], ["Named", "C.D"]]}]
        res = bridge.collect_io_signals(bodies)
        self.assertEqual(res["skipped_nameless"], 1)
        self.assertEqual(res["items"], [{"io_name": "Named", "io_path": "C.D"}])

    def test_dict_and_scalar_shapes(self):
        bodies = [{
            "input_signals": [{"name": "D", "path": "x.y"}],
            "expected_signals": ["Scalar"],
        }]
        res = bridge.collect_io_signals(bodies)
        self.assertEqual(res["items"], [
            {"io_name": "D", "io_path": "x.y"},
            {"io_name": "Scalar", "io_path": ""},
        ])

    def test_blank_and_nondict_bodies_counted_but_empty(self):
        res = bridge.collect_io_signals([{}, None, "junk", {"input_signals": []}])
        self.assertEqual(res["scanned_rows"], 4)
        self.assertEqual(res["distinct_signals"], 0)
        self.assertEqual(res["items"], [])

    def test_whitespace_trimmed(self):
        bodies = [{"input_signals": [["  Sig  ", "  A.B  "]]}]
        res = bridge.collect_io_signals(bodies)
        self.assertEqual(res["items"], [{"io_name": "Sig", "io_path": "A.B"}])


if __name__ == "__main__":
    unittest.main()
