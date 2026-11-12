# Qualification HTML reports

The project task page offers **HTML 测试报告** in the task detail dialog and
beside the batch download action. Select tasks to include in a combined report,
including pending tasks when they should appear in the Untested count.

Extract the downloaded `Qualification_Test_Report_<timestamp>.zip` and open
`Qualification_Test_Report_<timestamp>.html`. Its function link opens the detailed
report beside it. Keep both HTML files in the same directory. The report is
self-contained and works offline, with no web server or internet connection.

The templates preserve the supplied AutoTest Ver.03_01 report's CSS, embedded
logo, screen and print layout, Japanese labels, summary statistics, status
colors, case tables, check details and expand/collapse controls. Detail metadata
uses the original `親レポート参照` convention. The existing raw-result ZIP download
continues to provide the original runner artifacts.

The function name is the project name. Case names, purpose and category come
from the approved row captured with the execution attempt. Check target,
expected value, observed value, timing and result come from the latest exact
case section of `jdgrslt.log`, including nested subroutines. The report stops at
that case's final verdict to exclude unrelated teardown output. No graph or log
links are generated when their target files are absent from the HTML download.

Missing metadata displays `-`; missing logs are identified in the step table.
Queued and running attempts are Untested. Cancelled attempts with judge output
and runs without a conclusive judge result are T.B.D. A failing case makes the
overall banner fail; a selection containing unfinished cases cannot claim an
overall pass. Evidence kind is retained so mock output is identifiable.

Reports are generated in memory. Export never changes sealed evidence files.
Input/result integrity failures or a finalized attempt with no sealed outcome
reject the download. Legacy tasks use their existing task metadata and contained
log directory; they do not read current mutable matrix rows. All export endpoints
enforce the same `task.download` permission and project boundary as raw downloads.

Implementation: `app/services/qualification_report_service.py`,
`app/services/qualification_report_renderer.py`, and `app/templates/reports/`.
Targeted regression coverage: `tests/test_qualification_report.py` and
`tests/test_report_download.py`.

## Verification

Validated with CPython 3.10.18 and an isolated PostgreSQL 18 database: 84 tests
passed across qualification reporting, raw downloads, sealed execution evidence,
appended judge-output handling and route contracts. Both changed JavaScript files
passed `node --check`.

An isolated headless Edge browser rendered the original reports and generated
reports populated with the same 50 sample cases at 1440 x 1100. The overview and
detail viewport PNGs were byte-identical. CSS and embedded-logo hashes match the
original files. Case navigation, individual and global expand/collapse passed
with no browser errors in the generated reports. This compares the report
presentation; real Silver/license execution remains an internal validation step.

The complete GitHub CI suite and its PostgreSQL 16 integration gate still apply
before merge.
