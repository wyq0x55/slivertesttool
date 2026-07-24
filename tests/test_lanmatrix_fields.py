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


class SheetCatalogueTest(unittest.TestCase):
    """The unified identity protocol exposes one field catalogue per sheet
    (``test`` / ``const`` / ``lib`` / ``io``) instead of a separate system-field
    set."""

    def test_sheets_declared(self):
        self.assertEqual(set(fld.SHEETS), {"test", "const", "lib", "io"})
        self.assertIn(fld.DEFAULT_SHEET, fld.SHEETS)

    def test_required_test_keys_present(self):
        keys = {f["field_key"] for f in fld.TEST_FIELDS}
        self.assertEqual(keys, set(fld.TEST_FIELD_KEYS))
        for k in ("test_id", "test_name", "steps", "result",
                  "category", "test_no"):
            self.assertIn(k, keys)

    def test_every_field_has_supported_type(self):
        for sheet_fields in (fld.TEST_FIELDS, fld.CONST_FIELDS, fld.LIB_FIELDS,
                             fld.IO_FIELDS):
            for f in sheet_fields:
                self.assertIn(f["data_type"], fld.DATA_TYPES)
                self.assertIn(f["sheet"], fld.SHEETS)

    def test_steps_field_is_multiline_or_steps(self):
        steps = next(f for f in fld.TEST_FIELDS if f["field_key"] == "steps")
        self.assertIn(steps["data_type"], fld.MULTILINE_TYPES | {"steps"})


if __name__ == "__main__":
    unittest.main()
