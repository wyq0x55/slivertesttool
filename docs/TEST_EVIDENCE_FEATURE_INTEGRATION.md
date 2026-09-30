# Feature Integration Test Evidence

## Journeys

- Only a project administrator can commit a `replace_all` import.
- A queued task keeps the saved model copy and version selected at submission.
- A user can manage a specific saved model version and compare two versions.

## RED/GREEN

| Change | RED evidence | GREEN evidence |
| --- | --- | --- |
| Import authorization | 3 failed, 1 passed | `tests/test_import_replace_auth.py`: 4 passed |
| Model snapshots and identity | 4 regression failures | `tests/test_model_snapshot.py` plus `tests/test_task_staging_race.py`: 8 passed |
| Version comparison | 2 efficiency regressions failed | `tests/test_version_compare.py`: 11 passed |
| Test import isolation | Live model class was replaced and stub packages remained | Interval, staging, and comparison tests: 17 passed |

## Final Verification

- Full suite on the verified `silver_test_platform.lane_b` schema: 942 passed, 4 skipped, 4 existing SQLAlchemy warnings.
- `node --check` passed for the four changed dashboard/model scripts.
- `py_compile` passed for changed application and test modules.
- `git diff --check` passed; the focused security scan found no key-like strings or `console.log` calls.
- Coverage, Ruff, and Pyright were not run. The Vite build only emits vendor bundles; no Vite source changed.

## Follow-up: Import Preview and Run History

These checks extend the previous 942-test baseline. The final follow-up suite was rerun against a disposable PostgreSQL 18 cluster.

### Journeys

- A user previews any supported workbook format before commit; changing import mode generates a fresh preview.
- Invalid, expired, or stale previews cannot mutate rows; `replace_all` rechecks authorization and rolls back atomically.
- I/O upsert matches signal names case-insensitively while preserving path uniqueness.
- A project reader pages run history and can export the project history as CSV.

### RED/GREEN Evidence

| Guarantee | RED evidence | GREEN evidence |
| --- | --- | --- |
| Missing preview expiry/snapshot and a row inserted after snapshot validation are rejected | Targeted import run: 3 failed, 3 passed | `tests/test_import_replace_auth.py`: all import cases passed |
| Generic imports reject previews without expiry or snapshot | 2 targeted failures | `tests/test_import_replace_auth.py -k generic_import_rejects_missing_preview`: 2 passed |
| Case-insensitive I/O upsert commits against the existing row | Targeted import run: 1 failed, 3 passed | I/O identity/path regressions passed |
| Commit-time authorization, invalid-row blocking, expiry, stale snapshot, and transaction rollback | New focused regression cases | `tests/test_import_replace_auth.py` |
| PostgreSQL table lock rejects a conflicting writer, and a previewed I/O row commits | Added after the SQLite-only lock coverage was reviewed | `tests/test_import_postgresql_lock.py`: 2 passed |
| Run-history CSV download is registered in the public route contract | Full suite exposed a missing route-contract entry | `tests/test_lanmatrix_route_contract.py::test_projects_items_route_contract`: 1 passed |

- RED: `C:\workspeace\slivertesttool\.venv\Scripts\python.exe -m pytest -q tests/test_import_replace_auth.py -k 'special_replace_all_rechecks or special_import_blocks_invalid or incomplete_preview_guard or rows_added_after or rolls_back_all'` — 3 failed, 3 passed, 11 deselected.
- RED: `C:\workspeace\slivertesttool\.venv\Scripts\python.exe -m pytest -q tests/test_import_replace_auth.py -k 'expired_preview or snapshot_changed_before_commit or io_upsert_commits_case_insensitive or case_insensitive_path_collision'` — 1 failed, 3 passed, 17 deselected.

### Verification

- GREEN: `C:\workspeace\slivertesttool\.venv\Scripts\python.exe -m pytest -q tests/test_import_replace_auth.py tests/test_run_history.py tests/test_lanmatrix_excel.py tests/test_lanmatrix_io_excel.py tests/test_matrix_excel.py` — 46 passed, 3 skipped.
- GREEN: `tests/test_workspace_task_list.py` plus `tests/test_dashboard.py` — 57 passed.
- GREEN: Full single-process PostgreSQL suite — 978 passed, 4 skipped, 4 existing SQLAlchemy warnings in 61.20 seconds. Before pytest, `current_database()` and `current_schema()` were asserted as `postgres.lane_b` on a disposable local PostgreSQL 18 cluster.
- GREEN: `tests/test_import_postgresql_lock.py` exercised the real PostgreSQL lock manager and committed an I/O import from a preview job.
- `node --check app/static/js/lanmatrix/editor.js` and `git diff --check` passed.
- `py_compile` passed for changed Python modules; the changed-lines scan found no secret-like strings or `console.log` calls.
- The existing `silver_test_platform.lane_b` instance was not touched; its local service required a password that was not configured. The PostgreSQL run used an isolated temporary cluster and therefore verifies PostgreSQL behavior, not the real Silver database instance.
- Browser E2E, coverage, Ruff, and Pyright were not run. `pytest-cov`/`coverage.py` and frontend `node_modules` are unavailable; the preview wizard received syntax validation, not browser E2E validation.

## Follow-up: Task Report ZIP Downloads

### Journeys

- A project reader downloads one task report or selected reports as ZIP files, limited to the authorized project and the selected result trees.
- A result tree linked outside its expected workspace is not downloadable; linked files outside the tree are never included.

### RED/GREEN Evidence

| Guarantee | RED evidence | GREEN evidence |
| --- | --- | --- |
| Report ZIPs do not include files reached through a symlink or Windows directory junction | `test_report_download_does_not_follow_symlinks_outside_result_tree` failed after exposing the linked file | The same test passed after result-file containment checks |
| A result directory resolving outside `<workspace>/log/<test_id>` is rejected | `test_report_download_rejects_a_result_directory_link` returned 200 instead of 404 | The same test passed after validating the resolved result directory |
| Single and batch downloads enforce project membership, preserve selected files, and omit stored `report.zip` snapshots | Added route-level regression coverage | `tests/test_report_download.py`: 5 passed |

### Verification

- RED: the two path-boundary regressions reproduced external-file disclosure through Windows junctions.
- GREEN: `python -m pytest -q tests/test_report_download.py` — 5 passed.
- GREEN: Full suite against the disposable local PostgreSQL 18 `postgres.lane_b` target — 983 passed, 4 skipped, 4 existing SQLAlchemy warnings in 76.74 seconds.
- The `jdgrslt.log` lookup now shares the validated result-directory boundary and ignores symlink files. The real `silver_test_platform.lane_b` database was not used.
