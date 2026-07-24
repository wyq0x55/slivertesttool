"""入出力 (I/O signal pool) Excel parse / build round-trip tests.

Flask-independent: exercises ``io_excel`` (the openpyxl parser/builder) directly
through the synthetic pure-package loader, like ``test_lanmatrix_excel``.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from lm_helpers import load  # noqa: E402

lm = load()
io_excel = lm.io_excel
fld = lm.fields


class IoExcelRoundTripTest(unittest.TestCase):
    def _reparse(self, items):
        wb = io_excel.build_workbook({"items": items})
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return io_excel.parse_workbook(buf)["items"]

    def test_export_then_import_lossless(self):
        items = [
            {"io_name": "EngSpd", "io_path": "ECU.Engine.Speed", "io_note": "rpm"},
            {"io_name": "VehSpd", "io_path": "ECU.Vehicle.Speed", "io_note": ""},
        ]
        out = self._reparse(items)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], {"io_name": "EngSpd",
                                  "io_path": "ECU.Engine.Speed", "io_note": "rpm"})

    def test_blank_rows_dropped(self):
        items = [
            {"io_name": "", "io_path": "", "io_note": "junk"},
            {"io_name": "OnlyName", "io_path": "", "io_note": ""},
        ]
        out = self._reparse(items)
        self.assertEqual([o["io_name"] for o in out], ["OnlyName"])

    def test_japanese_header_aliases(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["名称", "パス", "備考"])
        ws.append(["Sig1", "A.B.C", "note"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        items = io_excel.parse_workbook(buf)["items"]
        self.assertEqual(items[0],
                         {"io_name": "Sig1", "io_path": "A.B.C", "io_note": "note"})

    def test_fields_catalogue_has_io(self):
        keys = {f["field_key"] for f in fld.IO_FIELDS}
        self.assertEqual(keys, {"io_name", "io_path", "io_note"})
        self.assertIn("io", fld.SHEETS)


if __name__ == "__main__":
    unittest.main()
