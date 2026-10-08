# CLI coverage verification — 2026-10-03

## Scope and source identity

Base: main `52d08823b123c8ee71b0cff837cc855734170dbc`.
Implementation: isolated `slivertesttool-wt-cli` detached worktree; no commit,
push, schema migration or production deployment is included.

Request: make existing platform operations accessible to AI CLI callers without
introducing a second business implementation. Plan: [CLI coverage](../roadmap/cli-coverage.md).
Operator contract: [CLI guide](../cli.md).

The original checkout remains at `79df20153a2631daa0d724def5186f1a52ed2feb`, with its
existing `CONTEXT.md` modification and untracked ADR preserved. No production DB,
real Silver process or paid AI provider was contacted during validation.

## Acceptance mapping

| Guarantee | Evidence |
|---|---|
| Every registered business API has a named CLI operation | `test_catalog_matches_registered_business_api`: exact Flask URL-map method/path parity; 138 operations, 15 groups |
| Offline discovery and preview | `test_offline_catalogue_without_site_packages`: subprocess `python -S`; all 138 operations execute dry-run with no Client construction |
| Original identity, CSRF and authorization remain authoritative | Loopback HTTP transport tests plus real Flask/PostgreSQL subprocess tests: owner edits, reader/outsider/system-admin rejection, 409 version conflicts |
| File and long-running workflows | Repeated multipart fields/files, streamed no-overwrite download/hash/partial cleanup, SSE resume semantics, explicit terminal states and bounded polling |
| Existing operational workflows remain usable | Four subprocess HTTP integration tests cover project/field/item edits, Excel export-preview-commit, AI draft wait/reject audit, saved-model submit/retest/cancel |

## TDD evidence

1. `tests/test_cli_runner.py`: initial execution before the package existed gave
   1 subprocess failure and 23 missing-implementation fixture errors; preserved
   in `.cache/cli/runner-red.log`. This establishes missing CLI behavior, not a
   claim that existing business code failed.
2. Catalogue/transport tests before their modules existed: **35 failed**;
   `.cache/cli/transport-red.log`. First complete three-module gate: **59 passed**.
3. Runner edge-case RED: **3 failed / 31 passed**, demonstrating accepted numeric
   overflow, token-count over-redaction and a stream-timeout incorrectly reporting
   success. All three corrected and covered by the same tests.
4. Local security-review RED: **2 failed / 2 passed**, demonstrating an echoed
   response token escaping redaction and a malformed login data string raising
   AttributeError. Both corrected; `.cache/cli/review-red.log` preserves evidence.
5. Final focused gate: **85 passed in 29.77 seconds**. All four live-loopback
   subprocess integration cases pass; no real Silver or live AI inference occurs.

The first item-edit integration assertions were corrected to the actual dynamic
field contract (custom fields are flattened in `TestItemRow.to_dict`). No unrelated
business field handling was modified to satisfy those test assumptions.

## Validation results

| Check | Observed result |
|---|---|
| New CLI unit/transport/integration suite | 85 passed; `.cache/cli/cli-final.log` |
| Combined line/branch coverage | 93%; catalogue 89%, runner 97%, transport 92%; `.cache/cli/coverage-final.txt` |
| `__main__` entry | Exercised by subprocess tests; not counted by parent-process coverage, shown as 0% for its two lines |
| Full backend regression | **1741 passed / 4 skipped / 4 existing SQLAlchemy warnings in 453.09 seconds**; `.cache/cli/full-regression-final.log`, process exit 0 in `.cache/cli/full-result.json` |
| Compile | `python -m compileall -q silver_cli` passed |
| Documentation examples | All 24 CLI example commands parse; referenced operation names resolve |
| Diff whitespace | Tracked diff and each untracked file checked with Git; no whitespace errors |
| Standalone lint/types | No standalone project checker configured; no success claimed |
| Frontend | Unchanged; no new frontend build/test claim |

The first full run was interrupted before a terminal summary. Its three visible
model-snapshot failures were reproduced by setting TEMP/TMP inside the repository:
saved model paths intentionally become project-relative there, while these older
tests expect absolute paths. Normal TEMP with workspace-local pytest `--basetemp`
passes all **21** model-directory/snapshot tests. The final full rerun uses normal
TEMP/TMP and passes; no production or unrelated test changes were made to mask this
condition.

### Reproducible environment

Python 3.10.18. Dedicated PostgreSQL 18 cluster under `.cache/cli/pgdata`, bound only
to loopback port 55436, database `cli_test`. Fixtures may erase that disposable DB.
Never reuse these commands with a production DSN. Coverage 7.10.7 was installed
only under ignored `.cache/cli/test-tools`, not added to application dependencies.
After the final gate, the listener's `SHOW data_directory` was checked against
this task's exact cluster directory before `pg_ctl -m fast -w stop`. The dedicated
server stopped successfully and port 55436 has no listener; evidence/data are kept.

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg2://postgres@127.0.0.1:55436/cli_test'
$env:DATABASE_URL = $env:TEST_DATABASE_URL
$env:HUEY_DATABASE_URL = $env:TEST_DATABASE_URL
$env:SECRET_KEY = 'cli-isolated-test-only'
$env:HUEY_IMMEDIATE = '1'
$env:RUNNER_BACKEND = 'mock'
$env:SILVER_POOL_ENABLED = '0'
$env:SILVER_POOL_PREWARM = '0'
python -m pytest -q -p no:cacheprovider --basetemp=.cache/cli/pytest-full-final
```

## Security review and limits

Local review covered no direct SQL/service bypass, explicit target URL, verified
HTTPS, no ambient proxies/redirects, in-memory session/CSRF, no automatic mutation
retries, pre-network write confirmation, secret redaction, file transfer boundaries,
and failure/timeout exit semantics. Relevant negative tests execute these paths.

Independent review agents were attempted but stopped on provider quota before
delivering a review. **This is local review, not an independent security sign-off.**

The CLI guarantees route reachability, not a complete schema for every payload or
real-target acceptance of all 138 operations. Permission/validation/CRDT conflicts
are returned, not bypassed. SSE end and task execution status alone do not establish
judge PASS. Real Silver, paid inference, browser collaboration, and production
deployment remain outside these test results.
