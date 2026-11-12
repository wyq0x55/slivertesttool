"""Apply the qualification workbook's presentation without copying test data.

The bundled asset contains only styles, sheet settings, schema labels and
numeric indexes. Keep the loaded workbook so its custom table styles, theme
and differential style IDs remain valid during serialization.
"""

from copy import copy, deepcopy
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter, range_boundaries


TEMPLATE_PATH = Path(__file__).with_name("templates") / "matrix_format.xlsx"


class MatrixFormat:
    def __init__(self):
        self.workbook = load_workbook(TEMPLATE_PATH)
        self.sheets = {ws.title: ws for ws in self.workbook}
        self.summary_table = deepcopy(self.sheets["4.TestRequirement"].tables["DB"])
        self.sheets["4.TestRequirement"].tables.clear()
        self.indexes = {}
        self.timing_columns = {}
        for name, definition in self.workbook.defined_names.items():
            if name.startswith("format_"):
                title, ref = next(definition.destinations)
                self.indexes[name] = (self.sheets[title], range_boundaries(ref))
        self.workbook.defined_names.clear()
        for i, ws in enumerate(self.sheets.values()):
            ws.title = f"__format_{i}"

    def finish(self):
        for ws in self.sheets.values():
            self.workbook.remove(ws)
        self.workbook.active = 0

    @staticmethod
    def _sheet(source, target):
        for attr in ("sheet_format", "sheet_properties", "views", "page_setup",
                     "page_margins", "print_options", "HeaderFooter",
                     "protection"):
            if hasattr(source, attr):
                setattr(target, attr, copy(getattr(source, attr)))
        target.page_setup._parent = target
        for key, dim in source.column_dimensions.items():
            target.column_dimensions[key] = copy(dim)
            target.column_dimensions[key].parent = target
        target.print_title_rows = source.print_title_rows
        target.print_title_cols = source.print_title_cols

    @staticmethod
    def _row(source, target, source_row, target_row, columns):
        if source_row in source.row_dimensions:
            dim = copy(source.row_dimensions[source_row])
            dim.index = target_row
            dim.parent = target
            target.row_dimensions[target_row] = dim
        else:
            target.row_dimensions.pop(target_row, None)
        for column, source_column in columns:
            target.cell(target_row, column)._style = copy(
                source.cell(source_row, source_column)._style)

    def summary(self, target, items):
        source = self.sheets["4.TestRequirement"]
        self._sheet(source, target)
        columns = [(c, c) for c in range(1, source.max_column + 1)]
        self._row(source, target, 1, 1, columns)
        for row, item in enumerate(items, 2):
            key = f"format_summary_{item.get('category')}_{item.get('test_no')}"
            reference = self.indexes.get(key)
            source_row = reference[1][1] if reference else 2
            self._row(source, target, source_row, row, columns)
        if source.print_area:
            min_col, min_row, max_col, _ = range_boundaries(
                str(source.print_area).rsplit("!", 1)[-1])
            target.print_area = (
                f"{get_column_letter(min_col)}{min_row}:"
                f"{get_column_letter(max_col)}{len(items) + 1}")
        table = deepcopy(self.summary_table)
        table.ref = target.tables["DB"].ref
        table.autoFilter.ref = table.ref
        del target.tables["DB"]
        target.add_table(table)

    def detail(self, target):
        source = self.sheets.get(target.title, self.sheets["1"])
        self._sheet(source, target)
        for row in (1, 2):
            self._row(source, target, row, row,
                      [(c, c) for c in range(1, source.max_column + 1)])

    def detail_widths(self, target):
        # Shared LibFunc styling writes widths while the values are generated.
        # Restore the qualification template's per-category column dimensions.
        source = self.sheets.get(target.title, self.sheets["1"])
        target.column_dimensions.clear()
        for key, dim in source.column_dimensions.items():
            target.column_dimensions[key] = copy(dim)
            target.column_dimensions[key].parent = target
        signals = target.column_dimensions.get("G")
        last_signal = self.timing_columns.get(target, 7)
        if signals and signals.min == 7 and 7 < signals.max < last_signal:
            old_end = signals.max
            signals.max = last_signal
            following_key = get_column_letter(old_end + 1)
            following = target.column_dimensions.pop(following_key, None)
            if following is not None:
                following.min = last_signal + 1
                following.index = get_column_letter(last_signal + 1)
                target.column_dimensions[following.index] = following

    def block(self, target, item, first, last, input_count, expected_count):
        suffix = f"{item.get('category')}_{item.get('test_no')}"
        reference = self.indexes.get(f"format_block_{suffix}")
        if reference is None:
            source = self.sheets.get(target.title, self.sheets["1"])
            name, reference = next((k, v) for k, v in self.indexes.items()
                                   if k.startswith("format_block_") and v[0] is source)
            suffix = name.removeprefix("format_block_")
        source, (_, source_first, _, source_last) = reference
        _, (_, header, _, step_last) = self.indexes[f"format_steps_{suffix}"]
        labels = [source.cell(header, c).value for c in range(7, source.max_column + 1)]
        n_input, n_expected = labels.count("入力値"), labels.count("期待値")
        source_timing = 7 + n_input + n_expected
        timing = 7 + input_count + expected_count
        self.timing_columns[target] = max(self.timing_columns.get(target, 7), timing)
        columns = []
        for c in range(1, max(timing, source.max_column + timing - source_timing) + 1):
            if c < 7:
                sc = c
            elif c < 7 + input_count:
                sc = 7 + min(c - 7, max(n_input - 1, 0))
            elif c < timing:
                sc = 7 + n_input + min(c - 7 - input_count, max(n_expected - 1, 0))
            else:
                sc = source_timing + c - timing
            columns.append((c, sc))
        for row in range(first, last + 1):
            sr = min(source_first + row - first, step_last)
            self._row(source, target, sr, row, columns)
        # Preserve the reference's blank separator and trailing formatted rows.
        for offset in range(1, source_last - step_last + 1):
            self._row(source, target, step_last + offset, last + offset, columns)
