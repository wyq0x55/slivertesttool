"""Data-type coercion + system-field catalogue tests."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from lm_helpers import load  # noqa: E402

lm = load()
fld = lm.fields


class CoercionTest(unittest.TestCase):
    def test_integer(self):
        self.assertEqual(fld.coerce_value("integer", "42"), 42)
        with self.assertRaises(fld.CoercionError):
            fld.coerce_value("integer", "4.5")

    def test_integer_rejects_bool(self):
        with self.assertRaises(fld.CoercionError):
            fld.coerce_value("integer", True)

    def test_decimal(self):
        self.assertEqual(fld.coerce_value("decimal", "3.14"), 3.14)

    def test_hex_variants(self):
        self.assertEqual(fld.coerce_value("hex", "0x7FF"), 0x7FF)
        self.assertEqual(fld.coerce_value("hex", "7ffh"), 0x7FF)
        self.assertEqual(fld.coerce_value("hex", 2047), 2047)
        with self.assertRaises(fld.CoercionError):
            fld.coerce_value("hex", "xyz")

    def test_boolean(self):
        self.assertTrue(fld.coerce_value("boolean", "yes"))
        self.assertFalse(fld.coerce_value("boolean", "否"))
        with self.assertRaises(fld.CoercionError):
            fld.coerce_value("boolean", "maybe")

    def test_date_and_datetime_iso(self):
        self.assertEqual(fld.coerce_value("date", "2024/03/05"), "2024-03-05")
        self.assertTrue(fld.coerce_value("datetime", "2024-03-05 10:30:00")
                        .startswith("2024-03-05T10:30"))

    def test_multi_select_split(self):
        self.assertEqual(fld.coerce_value("multi_select", "a; b,c"), ["a", "b", "c"])
        self.assertEqual(fld.coerce_value("multi_select", ["x", " y "]), ["x", "y"])

    def test_empty_numeric_is_none(self):
        self.assertIsNone(fld.coerce_value("integer", "  "))

    def test_unknown_type(self):
        with self.assertRaises(fld.CoercionError):
            fld.coerce_value("nope", "x")


class SystemFieldTest(unittest.TestCase):
    def test_required_keys_present(self):
        keys = {f["field_key"] for f in fld.SYSTEM_FIELDS}
        for k in ("case_id", "title", "test_steps", "expected_result", "result", "version"):
            self.assertIn(k, keys)

    def test_readonly_bookkeeping(self):
        vt = fld.system_field("version")
        self.assertTrue(vt["is_readonly"])
        self.assertNotIn("version", fld.EDITABLE_SYSTEM_KEYS)


if __name__ == "__main__":
    unittest.main()
