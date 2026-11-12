# Qualification matrix presentation

`matrix_format.xlsx` contains the presentation of the supplied
`Toyuta_DSP_Qualification_Test_SYS.xlsx`, limited to `4.TestRequirement` and
numeric category sheets. It is a runtime package asset, resolved relative to
`matrix_format.py`; deployments must include this directory.

The asset contains no test descriptions, IDs, signal names, values, formulas,
comments, hyperlinks, auxiliary sheets or external links. The only cell values
are summary column headers and procedure schema labels `入力値`, `期待値` and
`確認タイミング`. The header-only `DB` table retains table/column differential
formatting; its calculated formulas are removed.
Numeric defined names index summary rows and detail/procedure blocks by category
and test number, so formatting follows a test when preceding steps are edited.

The original font/fill/border/alignment/number formats, theme, custom table style,
row heights, category-specific column widths, views and print settings are
retained. Export loads the asset's workbook to keep style registries intact,
generates current editor values/formulas, then copies presentation only.
Additional steps/signals repeat the corresponding reference role's formatting;
new test numbers use the category's first block and new categories use category
1. Summary print ranges grow with the exported item count. Auxiliary sheets and
format indexes are not included in exports.

Empty summary fields, signal names/paths and procedure data cells are exported
as `-`. Numeric zero and boolean false remain typed values. Spacer rows, margins
and the blank parts of the multi-row procedure header remain layout whitespace.

Regenerate the asset explicitly from a trusted reference with the validated
project Python environment:

```text
python scripts/build_matrix_format_template.py REFERENCE.xlsx app/services/lanmatrix/templates/matrix_format.xlsx
```

Run `python -m unittest tests.test_matrix_format tests.test_matrix_excel` after
regeneration. Never replace the asset with an unsanitized customer workbook.
