"""Excel template / import / export round-trip tests (FR-EXCEL-*)."""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from lm_helpers import load  # noqa: E402

lm = load()
excel_io = lm.excel_io


def specs():
    return [
        {"field_key": "case_id", "display_name": "测试ID", "data_type": "text",
         "is_required": True, "is_readonly": False, "is_active": True},
        {"field_key": "title", "display_name": "标题", "data_type": "text",
         "is_required": True, "is_readonly": False, "is_active": True},
        {"field_key": "result", "display_name": "结果", "data_type": "single_select",
         "is_required": False, "is_readonly": False, "is_active": True,
         "options": ["Pass", "Fail"]},
    ]


class TemplateTest(unittest.TestCase):
    def test_build_template_hidden_key_row(self):
        wb = excel_io.build_template({"code": "P1"}, specs())
        ws = wb[excel_io.DATA_SHEET]
        self.assertEqual(ws.cell(excel_io.ROW_FIELD_KEY, 1).value, "case_id")
        self.assertTrue(ws.row_dimensions[excel_io.ROW_FIELD_KEY].hidden)
        self.assertIn(excel_io.INFO_SHEET, wb.sheetnames)


class RoundTripTest(unittest.TestCase):
    def test_export_then_import(self):
        rows = [
            {"case_id": "TC1", "title": "登录", "result": "Pass"},
            {"case_id": "TC2", "title": "登出", "result": "Fail"},
        ]
        wb = excel_io.build_export({"code": "P1"}, specs(), rows)
        buf = excel_io.workbook_bytes(wb)

        parsed = excel_io.parse_import(buf, specs())
        self.assertEqual(parsed["missing_required"], [])
        got = {r["values"]["case_id"]: r["values"]["title"] for r in parsed["rows"]}
        self.assertEqual(got, {"TC1": "登录", "TC2": "登出"})

    def test_formula_injection_escaped_on_export(self):
        rows = [{"case_id": "=DANGER()", "title": "x", "result": "Pass"}]
        wb = excel_io.build_export({"code": "P1"}, specs(), rows)
        ws = wb[excel_io.DATA_SHEET]
        self.assertEqual(ws.cell(excel_io.ROW_DATA_START, 1).value, "'=DANGER()")

    def test_import_reports_missing_required(self):
        rows = [{"title": "no id", "result": "Pass"}]
        # Export only title+result columns by restricting the spec set.
        partial = [s for s in specs() if s["field_key"] != "case_id"]
        wb = excel_io.build_export({"code": "P1"}, partial, rows)
        buf = excel_io.workbook_bytes(wb)
        parsed = excel_io.parse_import(buf, specs())
        self.assertIn("case_id", parsed["missing_required"])

    def test_non_seekable_stream(self):
        rows = [{"case_id": "TC1", "title": "x", "result": "Pass"}]
        wb = excel_io.build_export({"code": "P1"}, specs(), rows)
        data = excel_io.workbook_bytes(wb).getvalue()

        class NonSeekable:
            def __init__(self, b):
                self._b = b
                self.read_called = False

            def read(self, *a):
                self.read_called = True
                return self._b

            def seekable(self):
                raise AttributeError("no seekable")

        parsed = excel_io.parse_import(NonSeekable(data), specs())
        self.assertEqual(len(parsed["rows"]), 1)


if __name__ == "__main__":
    unittest.main()
