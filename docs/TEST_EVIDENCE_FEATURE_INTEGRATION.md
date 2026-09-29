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
