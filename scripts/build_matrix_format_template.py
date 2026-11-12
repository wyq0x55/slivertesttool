"""Extract a data-free presentation template from a qualification workbook.

Run with the reference workbook and output asset paths. Only column/procedure
schema labels and numeric formatting indexes survive; no test content is distributed.
"""

import argparse
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.packaging.core import DocumentProperties
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.utils import quote_sheetname


def build_template(source, destination):
    wb = load_workbook(source)
    summary = wb["4.TestRequirement"]
    wb.defined_names.clear()
    keep = [summary] + [s for s in wb if s.title.isdigit()]
    for ws in list(wb):
        if ws not in keep:
            wb.remove(ws)

    def index(name, ws, first, last):
        wb.defined_names.add(DefinedName(
            name, attr_text=f"{quote_sheetname(ws.title)}!$A${first}:$BR${last}"))

    for row in summary.iter_rows(min_row=2):
        category, number = row[1].value, row[4].value
        if isinstance(category, (int, float)) and isinstance(number, (int, float)):
            index(f"format_summary_{int(category)}_{int(number)}", summary,
                  row[0].row, row[0].row)
    headers = {}
    for cell in summary[1]:
        headers[summary.title, cell.coordinate] = cell.value
    for ws in keep[1:]:
        starts = [r for r in range(1, ws.max_row + 1)
                  if isinstance(ws.cell(r, 2).value, str)
                  and ws.cell(r, 2).value.startswith("ID;;")]
        for i, first in enumerate(starts):
            suffix = ws.cell(first, 2).value[-6:]
            category, number = int(suffix[:3]), int(suffix[3:])
            last = starts[i + 1] - 1 if i + 1 < len(starts) else ws.max_row
            step_header = next(r for r in range(first, last + 1)
                               if ws.cell(r, 2).value == "手順番号")
            step_end = step_header + 2
            while (step_end + 1 <= last
                   and ws.cell(step_end + 1, 2).value is not None):
                step_end += 1
            index(f"format_block_{category}_{number}", ws, first, last)
            index(f"format_steps_{category}_{number}", ws, step_header, step_end)
            for cell in ws[step_header]:
                if cell.value in {"入力値", "期待値", "確認タイミング"}:
                    headers[ws.title, cell.coordinate] = cell.value
    for ws in wb:
        ws.defined_names.clear()
        if ws is summary:
            table = ws.tables["DB"]
            table.ref = "A1:V1"
            table.autoFilter.ref = table.ref
            table.autoFilter.filterColumn.clear()
            table.autoFilter.sortState = None
            table.sortState = None
            for column in table.tableColumns:
                column.calculatedColumnFormula = None
                column.totalsRowFormula = None
        else:
            ws.tables.clear()
        ws._charts.clear()
        ws._images.clear()
        ws.data_validations.dataValidation.clear()
        ws.conditional_formatting._cf_rules.clear()
        for row in ws:
            for cell in row:
                cell.value = headers.get((ws.title, cell.coordinate))
                cell.comment = None
                cell.hyperlink = None
    wb._external_links.clear()
    wb.properties = DocumentProperties(creator="Test Platform")
    wb.custom_doc_props.props.clear()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    build_template(args.source, args.destination)
