"""Tests for the Flask-independent Excel codec (:mod:`app.services.lanmatrix.matrix_excel`).

These tests use only :mod:`openpyxl` + the stdlib ``unittest`` runner, so they
run without Flask / SQLAlchemy installed. They cover:

* parsing the reference workbook,
* a lossless parse -> build -> reparse round trip,
* export behaviour for summary-only items (no detail block), and
* the error path for a workbook without the ``DB`` summary table.

Run standalone::

    python -m unittest tests.test_matrix_excel
"""

from __future__ import annotations

import importlib.util
import io
import os
import unittest

from openpyxl import Workbook, load_workbook

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(
    _HERE, "..", "app", "services", "lanmatrix", "matrix_excel.py")
# Candidate locations for the reference workbook (repo copy or uploads).
_REFERENCE_CANDIDATES = [
    os.path.join(_HERE, "data", "VHILS1_RWS_MCU_Qualification_Test_SYS.xlsx"),
    "/app/uploads/VHILS1_RWS_MCU_Qualification_Test_SYS.xlsx",
]


def _load_module():
    spec = importlib.util.spec_from_file_location("matrix_excel", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mx = _load_module()


def _reference_path():
    for path in _REFERENCE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _with_export_placeholders(value):
    """The export contract represents absent procedure values with dashes."""
    if isinstance(value, dict):
        return {key: _with_export_placeholders(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_with_export_placeholders(v) for v in value]
    if value is None or (isinstance(value, str) and not value.strip()):
        return "-"
    return value


class RoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = _reference_path()

    def setUp(self):
        if not self.ref:
            self.skipTest("reference workbook not available")

    def test_parse_reference(self):
        parsed = mx.parse_workbook(self.ref, source_filename=os.path.basename(self.ref))
        self.assertTrue(parsed["items"], "expected at least one item")
        self.assertTrue(parsed["id_prefix"], "expected a detected id prefix")
        for item in parsed["items"]:
            self.assertIn("category", item)
            self.assertIn("test_no", item)
            self.assertIn("steps", item)

    def test_round_trip_lossless(self):
        parsed = mx.parse_workbook(self.ref, source_filename=os.path.basename(self.ref))
        wb = mx.build_workbook(parsed)
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        reparsed = mx.parse_workbook(buffer, source_filename=os.path.basename(self.ref))

        self.assertEqual(len(parsed["items"]), len(reparsed["items"]))
        self.assertEqual(parsed["id_prefix"], reparsed["id_prefix"])

        for a, b in zip(parsed["items"], reparsed["items"]):
            for key in ("category", "test_no", "test_name", "priority", "result"):
                self.assertEqual(a.get(key), b.get(key), f"mismatch on {key}")
            self.assertEqual(_with_export_placeholders(a.get("steps")),
                             b.get("steps"), "steps mismatch")


class BuildTest(unittest.TestCase):
    def _summary_only_matrix(self):
        return {
            "name": "unit",
            "source_filename": "unit.xlsx",
            "summary_sheet": mx.DEFAULT_SUMMARY_SHEET,
            "id_prefix": "ID;;PFX-",
            "items": [
                {
                    "category": 1,
                    "test_no": 1,
                    "test_name": "summary only",
                    "priority": "\u4e0d\u8981",
                    "result": "-",
                    "steps": {"input_signals": [], "expected_signals": [], "steps": []},
                }
            ],
        }

    def test_summary_only_item_has_no_detail_sheet(self):
        wb = mx.build_workbook(self._summary_only_matrix())
        # Only the summary sheet exists; no numeric detail sheet was created.
        self.assertIn(mx.DEFAULT_SUMMARY_SHEET, wb.sheetnames)
        numeric = [s for s in wb.sheetnames if s.strip().isdigit()]
        self.assertEqual(numeric, [], "summary-only item must not produce a detail sheet")

    def test_build_reparse_summary_only(self):
        wb = mx.build_workbook(self._summary_only_matrix())
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        parsed = mx.parse_workbook(buffer, source_filename="unit.xlsx")
        self.assertEqual(len(parsed["items"]), 1)
        item = parsed["items"][0]
        self.assertEqual(item["category"], 1)
        self.assertEqual(item["test_no"], 1)
        self.assertEqual(item["steps"]["steps"], [])


class EmptyExportCellTest(unittest.TestCase):
    def test_summary_missing_values_use_dashes_and_dates_stay_typed(self):
        import datetime

        wb = Workbook()
        ws = wb.active
        mx._write_summary_sheet(ws, [
            {"category": 7, "test_no": 1, "test_name": " ",
             "description": 0, "remark": False},
            {"category": 7, "test_no": 2, "exec_date": "2026-11-12"},
            {"test_name": "No identity"},
        ], "ID;;UNIT-")
        stream = io.BytesIO()
        wb.save(stream)
        ws = load_workbook(stream).active
        for row in ws.iter_rows(min_row=2, max_row=4, max_col=22):
            self.assertTrue(all(cell.value is not None for cell in row))
        self.assertEqual(ws["F2"].value, "-")
        self.assertEqual(ws["O2"].value, "-")
        self.assertEqual(ws["M2"].value, 0)
        self.assertIs(ws["T2"].value, False)
        self.assertEqual(ws["A4"].value, "-")
        self.assertEqual(ws["O3"].value, datetime.datetime(2026, 11, 12))
        self.assertTrue(ws["A2"].value.startswith('="ID;;UNIT-"'))

    def test_detail_missing_data_use_dashes_but_layout_stays_blank(self):
        wb = Workbook()
        ws = wb.active
        mx._write_detail_block(ws, 3, {
            "category": 7, "test_no": 1, "_id_prefix": "ID;;UNIT-",
            "steps": {
                "input_signals": [["signal", ""], [None, None]],
                "expected_signals": [["expected", " "]],
                "steps": [{"no": 1, "purpose": " ", "inputs": [0, False]}, {}],
            },
        })
        stream = io.BytesIO()
        wb.save(stream)
        ws = load_workbook(stream).active
        self.assertEqual(ws["G21"].value, 0)
        self.assertIs(ws["H21"].value, False)
        for row in (21, 22):
            for col in range(2, 11):
                if (row, col) not in {(21, 2), (21, 7), (21, 8)}:
                    self.assertEqual(ws.cell(row, col).value, "-")
        for coordinate in ("G20", "H19", "H20", "I20"):
            self.assertEqual(ws[coordinate].value, "-")
        # The fixed header columns form a tall visual group; separators and
        # margins are layout whitespace rather than missing business values.
        for coordinate in ("A21", "B19", "B20", "B17", "C3"):
            self.assertIsNone(ws[coordinate].value)
        self.assertEqual(ws["C11"].value,
                         '=": "&IF(VLOOKUP(B3,DB[#Data],COLUMN(DB[実施日]),0)="-",'
                         '"-",TEXT(VLOOKUP(B3,DB[#Data],COLUMN(DB[実施日]),0),"yyyy/mm/dd"))')


class _NoSeekableStream:
    """A minimal file-like without ``seekable`` (mimics Werkzeug uploads)."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, *a):
        return self._buf.read(*a)

    def seek(self, *a):
        return self._buf.seek(*a)


class UploadStreamTest(unittest.TestCase):
    def setUp(self):
        self.ref = _reference_path()
        if not self.ref:
            self.skipTest("reference workbook not available")

    def test_parse_stream_without_seekable(self):
        with open(self.ref, "rb") as fh:
            data = fh.read()
        stream = _NoSeekableStream(data)
        parsed = mx.parse_workbook(stream, source_filename="x.xlsx")
        self.assertTrue(parsed["items"])


class ErrorTest(unittest.TestCase):
    def test_missing_db_table_raises(self):
        wb = Workbook()
        wb.active.title = "Sheet1"
        wb.active["A1"] = "not a matrix"
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        with self.assertRaises(mx.MatrixExcelError):
            mx.parse_workbook(buffer, source_filename="bad.xlsx")


if __name__ == "__main__":
    unittest.main()
