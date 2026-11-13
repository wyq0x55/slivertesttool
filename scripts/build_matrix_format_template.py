"""Extract a data-free presentation template from a qualification workbook.

Run with the reference workbook and output asset paths. Only column/procedure
schema labels and numeric formatting indexes survive; no test content is distributed.
"""

import argparse
import io
import zipfile
import xml.etree.ElementTree as ET
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
    prune_cell_styles(destination)


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def prune_cell_styles(path) -> tuple[int, int]:
    """Drop named cell styles that no cell format references, in place.

    Reference workbooks that were copy-pasted between files for years carry
    tens of thousands of orphaned named styles ("20% - Accent 1 10 2", ...).
    openpyxl expands every one of them on load and writes every one back on
    save, which made each export and re-import of a few hundred tests take
    15+ seconds. Only ``cellXfs/@xfId`` (plus the mandatory Normal style at
    index 0) references ``cellStyleXfs``, so the unreferenced entries and
    their ``cellStyle`` names can be removed and the survivors renumbered
    without changing how any cell looks. Returns (before, after) counts.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as zin:
        entries = [(info, zin.read(info.filename)) for info in zin.infolist()]

    ns = {"m": _MAIN_NS}
    ET.register_namespace("", _MAIN_NS)
    out = io.BytesIO()
    before = after = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in entries:
            if info.filename == "xl/styles.xml":
                root = ET.fromstring(data)
                style_xfs = root.find("m:cellStyleXfs", ns)
                cell_xfs = root.find("m:cellXfs", ns)
                names = root.find("m:cellStyles", ns)
                parents = list(style_xfs)
                before = len(parents)
                used = {0} | {int(xf.get("xfId", 0)) for xf in cell_xfs}
                remap = {old: new for new, old in enumerate(sorted(used))}
                for old, xf in enumerate(parents):
                    if old not in remap:
                        style_xfs.remove(xf)
                style_xfs.set("count", str(len(remap)))
                for xf in cell_xfs:
                    xf.set("xfId", str(remap[int(xf.get("xfId", 0))]))
                if names is not None:
                    for style in list(names):
                        old = int(style.get("xfId", 0))
                        if old in remap:
                            style.set("xfId", str(remap[old]))
                        else:
                            names.remove(style)
                    names.set("count", str(len(names)))
                after = len(remap)
                data = ET.tostring(root, xml_declaration=True, encoding="UTF-8")
            zout.writestr(info, data)
    path.write_bytes(out.getvalue())
    return before, after


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="reference workbook, or the asset with --prune-only")
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--prune-only", action="store_true",
                        help="only drop unreferenced named styles from SOURCE in place")
    args = parser.parse_args()
    if args.prune_only:
        print("cell styles: %d -> %d" % prune_cell_styles(args.source))
    else:
        if not args.destination:
            parser.error("destination is required unless --prune-only is given")
        build_template(args.source, args.destination)
