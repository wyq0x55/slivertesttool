"""Field validation engine tests (FR-GRID-005)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from lm_helpers import load  # noqa: E402

lm = load()
validation = lm.validation
FieldSpec = validation.FieldSpec


def spec(**kw):
    kw.setdefault("field_key", "f")
    kw.setdefault("data_type", "text")
    return FieldSpec(**kw)


class ValueValidationTest(unittest.TestCase):
    def test_required_empty(self):
        _, errs = validation.validate_value(spec(is_required=True), "")
        self.assertTrue(validation.has_blocking(errs))

    def test_optional_empty_ok(self):
        val, errs = validation.validate_value(spec(), "")
        self.assertFalse(validation.has_blocking(errs))

    def test_length_rules(self):
        s = spec(rule={"min_length": 2, "max_length": 4})
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "a")[1]))
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "abcde")[1]))
        self.assertFalse(validation.has_blocking(validation.validate_value(s, "abc")[1]))

    def test_numeric_range(self):
        s = spec(data_type="integer", rule={"min": 1, "max": 10})
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "0")[1]))
        self.assertFalse(validation.has_blocking(validation.validate_value(s, "5")[1]))

    def test_pattern(self):
        s = spec(rule={"pattern": r"[A-Z]{3}"})
        self.assertFalse(validation.has_blocking(validation.validate_value(s, "ABC")[1]))
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "abc")[1]))

    def test_enum_single_select(self):
        s = spec(data_type="single_select", options=["Pass", "Fail"])
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "Nope")[1]))
        self.assertFalse(validation.has_blocking(validation.validate_value(s, "Pass")[1]))

    def test_can_std_range(self):
        s = spec(data_type="hex", rule={"can_std_id": True})
        self.assertFalse(validation.has_blocking(validation.validate_value(s, "0x7FF")[1]))
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "0x800")[1]))

    def test_can_ext_range(self):
        s = spec(data_type="hex", rule={"can_ext_id": True})
        self.assertFalse(validation.has_blocking(validation.validate_value(s, "0x1FFFFFFF")[1]))
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "0x20000000")[1]))

    def test_timeout_range(self):
        s = spec(data_type="integer", rule={"timeout_min": 10, "timeout_max": 100})
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "5")[1]))
        self.assertTrue(validation.has_blocking(validation.validate_value(s, "200")[1]))

    def test_control_chars_blocked(self):
        _, errs = validation.validate_value(spec(), "a\x01b")
        self.assertTrue(validation.has_blocking(errs))

    def test_unique_checker(self):
        s = spec(rule={"unique": True})
        _, errs = validation.validate_value(s, "dup", unique_checker=lambda k, v: False)
        self.assertTrue(validation.has_blocking(errs))


class RecordValidationTest(unittest.TestCase):
    def test_cross_field_date_before(self):
        specs = [
            spec(field_key="start", data_type="date", rule={"date_before": "end"}),
            spec(field_key="end", data_type="date"),
        ]
        _, errs = validation.validate_record(
            specs, {"start": "2024-05-01", "end": "2024-04-01"})
        self.assertTrue(validation.has_blocking(errs))
        _, ok = validation.validate_record(
            specs, {"start": "2024-04-01", "end": "2024-05-01"})
        self.assertFalse(validation.has_blocking(ok))

    def test_readonly_skipped(self):
        specs = [spec(field_key="version", data_type="integer",
                      is_readonly=True, is_required=True)]
        coerced, errs = validation.validate_record(specs, {})
        self.assertNotIn("version", coerced)
        self.assertFalse(validation.has_blocking(errs))

    def test_draft_skips_required(self):
        # BUG A regression: a blank draft row must not fail required checks.
        specs = [
            spec(field_key="case_id", is_required=True),
            spec(field_key="title", is_required=True),
            spec(field_key="result", data_type="single_select",
                 options=["Pass", "Fail"], is_required=True),
        ]
        _, blocked = validation.validate_record(specs, {})
        self.assertTrue(validation.has_blocking(blocked))
        _, draft = validation.validate_record(specs, {}, enforce_required=False)
        self.assertFalse(validation.has_blocking(draft))

    def test_draft_still_validates_bad_values(self):
        # Draft mode relaxes "required" but still rejects invalid provided data.
        s = spec(data_type="integer")
        _, errs = validation.validate_value(s, "not-int", enforce_required=False)
        self.assertTrue(validation.has_blocking(errs))


class TemplateFieldsTest(unittest.TestCase):
    def test_template_fields_present_and_valid(self):
        flds = lm.fields
        keys = {f["field_key"] for f in flds.TEMPLATE_FIELDS}
        # Test-Matrix based columns are seeded on every project.
        for expected in ("category", "test_no", "purpose", "steps",
                         "traceability_id", "upper_req_id"):
            self.assertIn(expected, keys)
        # Every template field uses a supported data type.
        for f in flds.TEMPLATE_FIELDS:
            self.assertIn(f["data_type"], flds.DATA_TYPES)
        # Template fields must not collide with system field keys.
        self.assertFalse(keys & flds.SYSTEM_FIELD_KEYS)


class TestMatrixBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tb = lm.testmatrix_bridge

    def test_map_item_basic_columns(self):
        tm = {
            "category": 3, "category_name": "機能", "viewpoint": "正常系",
            "test_no": 12, "test_name": "起動確認", "purpose": "目的X",
            "priority": "高", "result": "OK", "remark": "備考Y",
            "traceability_id": "TR-1", "upper_req_id": "REQ-9",
            "steps": {"input_signals": [], "expected_signals": [], "steps": []},
        }
        v = self.tb.map_item(tm)
        self.assertEqual(v["category"], 3)
        self.assertEqual(v["test_no"], 12)
        self.assertEqual(v["title"], "起動確認")       # test_name -> title
        self.assertEqual(v["comment"], "備考Y")         # remark -> comment
        self.assertEqual(v["priority"], "High")         # 高 -> High
        self.assertEqual(v["result"], "Pass")           # OK -> Pass
        self.assertNotIn("steps", v)                     # empty steps omitted

    def test_map_item_serialises_steps(self):
        tm = {"category": 1, "test_no": 1, "steps": {
            "input_signals": [["sig", "a/b"]], "expected_signals": [],
            "steps": [{"no": 1, "operation": "do"}]}}
        v = self.tb.map_item(tm)
        self.assertIn("steps", v)
        doc = self.tb.parse_steps(v["steps"])
        self.assertEqual(doc["input_signals"], [["sig", "a/b"]])
        self.assertEqual(doc["steps"][0]["operation"], "do")

    def test_result_defaults_not_tested(self):
        self.assertEqual(self.tb.normalize_result(None), "Not Tested")
        self.assertEqual(self.tb.normalize_result("謎"), "Not Tested")
        self.assertEqual(self.tb.normalize_result("NG"), "Fail")

    def test_priority_unknown_empty(self):
        self.assertEqual(self.tb.normalize_priority("中"), "Medium")
        self.assertEqual(self.tb.normalize_priority(None), "")
        self.assertEqual(self.tb.normalize_priority("???"), "")

    def test_reconstruct_case_id(self):
        self.assertEqual(
            self.tb.reconstruct_case_id("ID;;", {"category": 3, "test_no": 12}),
            "ID;;003012")
        self.assertEqual(
            self.tb.reconstruct_case_id("ID;;", {"category": None, "test_no": 1}),
            "")

    def test_roundtrip_priority_result(self):
        # lanmatrix row -> Test-Matrix item translates enums back to Japanese.
        row = {"priority": "High", "result": "Pass", "title": "T",
               "comment": "note"}
        tm = self.tb.lm_to_tm(row)
        self.assertEqual(tm["priority"], "高")
        self.assertEqual(tm["result"], "OK")
        self.assertEqual(tm["test_name"], "T")
        self.assertEqual(tm["remark"], "note")

    def test_parse_steps_bad_json(self):
        empty = self.tb.parse_steps("{not json")
        self.assertEqual(empty["steps"], [])
        self.assertEqual(empty["input_signals"], [])


if __name__ == "__main__":
    unittest.main()
