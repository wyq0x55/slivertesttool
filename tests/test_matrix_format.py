"""Mandatory presentation regression tests using the bundled data-free asset."""

import io
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from openpyxl import load_workbook

from tests.test_matrix_excel import mx


def item(category=1, number=5, inputs=51, expected=12, count=6):
    return {
        "category": category, "test_no": number, "test_name": "Edited test",
        "purpose": "Current editor content", "priority": "高",
        "steps": {
            "input_signals": [[f"input{i}", f"model.input{i}"] for i in range(inputs)],
            "expected_signals": [[f"output{i}", f"model.output{i}"] for i in range(expected)],
            "steps": [{"no": i + 1, "purpose": "Edited step", "operation": "Run",
                       "inputs": [i] * inputs, "expecteds": [i + 1] * expected,
                       "timing": "end"} for i in range(count)],
        },
    }


def build(items):
    return mx.build_workbook({"id_prefix": "ID;;UNIT-", "items": items})


class MatrixFormatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = build([item(), item(4, 2), item(9, 1, 1, 1, 2)])
        cls.template = load_workbook(
            Path(mx.__file__).with_name("templates") / "matrix_format.xlsx")

    def test_reference_cell_styles_and_heights(self):
        for name in ("1", "4"):
            actual, reference = self.wb[name], self.template[name]
            for row in reference.iter_rows(min_row=1, max_row=27):
                for cell in row:
                    self.assertEqual(actual[cell.coordinate]._style, cell._style,
                                     f"{name}!{cell.coordinate}")
            for r in range(1, 28):
                self.assertEqual(actual.row_dimensions[r].height,
                                 reference.row_dimensions[r].height)

    def test_sheet_settings_and_per_category_widths(self):
        self.assertEqual(self.wb.sheetnames, ["4.TestRequirement", "1", "4", "9"])
        for name in ("4.TestRequirement", "1", "4"):
            actual, reference = self.wb[name], self.template[name]
            self.assertFalse(actual.sheet_view.showGridLines)
            self.assertEqual(actual.sheet_format, reference.sheet_format)
            self.assertEqual(actual.page_setup, reference.page_setup)
            self.assertEqual(actual.page_margins, reference.page_margins)
            for key, dimension in reference.column_dimensions.items():
                self.assertEqual(dict(actual.column_dimensions[key]), dict(dimension))
        self.assertEqual(self.wb["1"].sheet_view.zoomScale, 80)
        self.assertEqual(self.wb["4"].column_dimensions["D"].width, 50.375)
        self.assertEqual(self.wb["9"].freeze_panes, "E1")

    def test_generated_values_and_formulas_survive_styling(self):
        self.assertEqual(self.wb["1"]["B3"].value, "ID;;UNIT-001005")
        self.assertEqual(self.wb["1"]["G21"].value, 0)
        self.assertEqual(self.wb["1"]["D3"].value,
                         "=VLOOKUP(B3,DB[#Data],COLUMN(DB[テスト名]),0)")
        summary = self.wb["4.TestRequirement"]
        self.assertEqual(summary["F2"].value, "Edited test")
        self.assertEqual(summary["G2"].value, "Current editor content")
        self.assertEqual(summary["A2"].value, '=\"ID;;UNIT-\"&TEXT(B2,\"000\")&TEXT(E2,\"000\")')
        self.assertEqual(summary.tables["DB"].ref, "A1:V4")
        self.assertEqual(summary.print_area, "'4.TestRequirement'!$A$1:$M$4")
        self.assertEqual(len(self.wb.defined_names), 0)

    def test_custom_table_style_and_theme_survive_serialization(self):
        stream = io.BytesIO()
        self.wb.save(stream)
        loaded = load_workbook(stream)
        self.assertEqual(loaded["4.TestRequirement"].tables["DB"].tableStyleInfo.name,
                         "TestReqTable")
        self.assertEqual(loaded["4.TestRequirement"].tables["DB"].headerRowDxfId, 25)
        self.assertEqual(loaded["4.TestRequirement"].tables["DB"].dataDxfId, 23)
        for actual, reference in zip(loaded["4.TestRequirement"].tables["DB"].tableColumns,
                                     self.template["4.TestRequirement"].tables["DB"].tableColumns):
            self.assertEqual(actual, reference)
        self.assertEqual(loaded.loaded_theme, self.template.loaded_theme)
        with zipfile.ZipFile(stream) as archive:
            root = ElementTree.fromstring(archive.read("xl/styles.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        styles = root.findall("s:tableStyles/s:tableStyle", ns)
        custom = next(s for s in styles if s.attrib["name"] == "TestReqTable")
        dxfs = root.find("s:dxfs", ns)
        for element in custom:
            self.assertLess(int(element.attrib["dxfId"]), len(dxfs))

    def test_asset_contains_only_schema_and_numeric_indexes(self):
        self.assertEqual(self.template.sheetnames, ["4.TestRequirement", "1", "2", "3", "4"])
        allowed = {"入力値", "期待値", "確認タイミング"} | set(mx.SUMMARY_HEADERS)
        for sheet in self.template:
            self.assertFalse(sheet.defined_names)
            if sheet.title == "4.TestRequirement":
                table = sheet.tables["DB"]
                self.assertEqual(table.ref, "A1:V1")
                for column in table.tableColumns:
                    self.assertIsNone(column.calculatedColumnFormula)
                    self.assertIsNone(column.totalsRowFormula)
            else:
                self.assertFalse(sheet.tables)
            for row in sheet:
                for cell in row:
                    self.assertIn(cell.value, allowed | {None})
                    self.assertIsNone(cell.comment)
                    self.assertIsNone(cell.hyperlink)
        for name in self.template.defined_names:
            self.assertRegex(name, r"^format_(summary|block|steps)_\d+_\d+$")


class MatrixFormatExtensionTest(unittest.TestCase):
    def test_empty_export_keeps_summary_only(self):
        wb = build([])
        self.assertEqual(wb.sheetnames, ["4.TestRequirement"])
        stream = io.BytesIO()
        wb.save(stream)
        self.assertEqual(load_workbook(stream).active["A1"].value, "テストID")

    def test_changed_signal_counts_and_longer_blocks(self):
        cases = [item(inputs=0, expected=0, count=9),
                 item(number=999, inputs=1, expected=2, count=8),
                 item(number=1000, inputs=75, expected=3, count=2)]
        wb = build(cases)
        ws = wb["1"]
        starts = [r for r in range(1, ws.max_row + 1)
                  if str(ws.cell(r, 2).value).startswith("ID;;")]
        self.assertEqual(starts, [3, 31, 58])
        for start, case in zip(starts, cases):
            n_input = len(case["steps"]["input_signals"])
            n_expected = len(case["steps"]["expected_signals"])
            header = start + 15
            timing = 7 + n_input + n_expected
            self.assertEqual(ws.cell(header, timing).value, "確認タイミング")
            self.assertEqual(ws.cell(header, timing).fill.fgColor.rgb, "FFCCECFF")
            if n_input:
                self.assertEqual(ws.cell(header, 7).fill.fgColor.rgb, "FFCCFFCC")
            if n_expected:
                self.assertEqual(ws.cell(header, 7 + n_input).fill.fgColor.rgb,
                                 "FFCCECFF")
            last = header + 2 + len(case["steps"]["steps"])
            self.assertEqual(ws.cell(last, 3).value, "Edited step")
            self.assertIsNotNone(ws.cell(last, timing).border.bottom.style)
        self.assertEqual(ws.column_dimensions["G"].max, 85)
        self.assertEqual(ws.column_dimensions["G"].width, 22)
        self.assertEqual(ws.column_dimensions["CH"].min, 86)
        stream = io.BytesIO()
        wb.save(stream)
        loaded = load_workbook(stream)["1"]
        for col in range(71, 86):
            dimension = next(d for d in loaded.column_dimensions.values()
                             if d.min <= col <= d.max)
            self.assertEqual(dimension.width, 22)


if __name__ == "__main__":
    unittest.main()
