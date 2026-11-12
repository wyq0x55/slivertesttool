# Qualification HTML reports

The project task page offers **项目 HTML 测试报告** in the toolbar and task detail
dialog. No case selection is required. Every export covers the complete current
project test matrix, including rows that have never been run and rows marked as
not requiring a test. Task-table filters, pagination and checkbox selections do
not limit the report.

Extract the downloaded `Qualification_Test_Report_<timestamp>.zip` and open
`Qualification_Test_Report_<timestamp>.html`. Its function link opens the detailed
report beside it. Keep both HTML files in the same directory. The report is
self-contained and works offline, with no web server or internet connection.

The templates preserve the supplied AutoTest Ver.03_01 report's CSS, embedded
logo, screen and print layout, Japanese labels, summary statistics, status
colors, case tables, check details and expand/collapse controls. The nine
`System Under Test` fields display exactly `-` in both overview and detail pages.
No owner, version, date, evidence label or parent-report reference is inserted.
The existing raw-result ZIP download continues to provide the original runner
artifacts.

The function name is the project name. Cases follow matrix row order, without
removing duplicate or blank case IDs. Soft-deleted rows and const/lib/io sheets
are excluded. Each row is paired with its latest non-deleted task, without a
recent-task window. Removed-case tasks do not add rows to the report. A pending
retest does not inherit an earlier run's result.

Executed case names, purpose and category come from the approved row captured
with the execution attempt. Unrun cases use their current matrix fields. Check target,
expected value, observed value, timing and result come from the latest exact
case section of `jdgrslt.log`, including nested subroutines. The report stops at
that case's final verdict to exclude unrelated teardown output. No graph or log
links are generated when their target files are absent from the HTML download.

All unrun rows, including queued/running attempts and not-required cases, display
`Untested`. This is a report presentation rule and does not change exemption
approvals, task states or stored matrix results. Untested is included in the total
and not-executed counts. Passed, failed and T.B.D cases contribute to executed
statistics; Untested cases never count as passed. Cancelled attempts with judge
output and runs without a conclusive judge result are T.B.D. A failing case makes
the overall banner fail. An all-Untested report uses a neutral banner; a report
whose executed cases all passed uses the passed banner. Missing values and empty
step tables display `-` without adding explanatory rows.

Reports are generated in memory. Export never changes sealed evidence files.
Input/result integrity failures or a finalized attempt with no sealed outcome
reject the download. Legacy tasks use their existing task metadata and contained
log directory. Exports read the materialized project rows, using the same read
boundary as existing matrix/Excel exports; they never materialize live Y.Doc data.
All export endpoints
enforce the same `task.download` permission and project boundary as raw downloads.

Implementation: `app/services/qualification_report_service.py`,
`app/services/qualification_report_renderer.py`, and `app/templates/reports/`.
Targeted regression coverage: `tests/test_qualification_report.py` and
`tests/test_report_download.py`.

## Verification

Targeted checks cover whole-project exports without selection, more than 200
cases, not-required and never-run rows, latest attempts, project isolation,
excluded sheets/deleted rows, duplicate IDs, dash-only metadata on both pages,
original CSS/logo hashes and sealed-evidence integrity. Browser verification
checks the requested System Under Test layout, Untested display and offline case
navigation/expansion. Real Silver/license execution remains an internal
validation step.

The complete GitHub CI suite and its PostgreSQL 16 integration gate still apply
before merge.
