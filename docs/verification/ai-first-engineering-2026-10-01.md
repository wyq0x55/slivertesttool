# AI-first engineering verification — October 1-2, 2026

Status: local engineering gates verified. GitHub PR and main checks are the
authoritative integration record; this is not real-target acceptance.
Plan: [AI-first execution](../roadmap/ai-first-execution.md).
Implementation contract: [engineering delivery](../roadmap/ai-first-delivery.md).

## Environment and safety

- CPython 3.10.18; dedicated PostgreSQL 18 instance on port 55432.
- Main, approval, evidence/recovery and browser lanes use separate disposable databases.
- Root checkout changes to `CONTEXT.md` and ADR 0003 are preserved.
- No real Silver run or external AI request is used for these engineering checks.
- M0 PR #34 and its main CI completed successfully before this integration.

## Executed gates

| Guarantee | Executed target | Observed result |
| --- | --- | --- |
| Server context, fenced jobs, atomic approvals and existing AI behavior | `test_ai_api.py test_ai_context.py test_ai_jobs.py test_ai_approval.py test_ai_unit.py test_worker_attempts.py` | 221 passed in 85.89 seconds before runtime-conversion integration |
| Matrix selection, recovery controls, stale responses and partial approval UI | `node --test frontend/tests/ai-workflow.test.cjs` | 59 passed |
| All frontend regression contracts | `npm test` | 73 passed |
| Univer, collaboration and SBS production bundles | `npm run build` | Passed; local generated bundle drift was not included in the source patch |
| Runner conversion, isolated archives, history and writeback | Evidence lane: `test_run_evidence.py test_export_validation.py test_run_writeback_db.py test_run_history.py` | 66 passed in 14.97 seconds |
| Actual runner validation in AI generation/edit/approval | New runtime tests and exporter tests | 12 RED regressions; 30 passed after integration |
| Async queued-attempt recovery and compact progress summaries | Worker recovery, worker claims and AI jobs | 4 RED boundaries; 17 passed in 9.39 seconds |
| Archived failure analysis and approval review | Failure-focused analysis/approval/evidence target | 12 RED boundaries; 28 passed in 18.09 seconds |
| Arithmetic and aggregate log-read budgets | Focused resource-limit tests | 4 RED regressions; 4 passed |
| Trusted archive metadata | Database attempt metadata tests | 3 RED integrity regressions; 3 passed, including coordinated file/hash rewriting |
| Retry after submission rollback | Focused evidence target | RED archive collision; 4 passed including metadata checks |
| Atomic terminal state/history and durable writeback | Finalisation, evidence, history and PostgreSQL writeback | 3 RED boundaries; 56 passed in 20.55 seconds |
| Sealed duplicate delivery and finalisation crash | Finalisation, evidence and worker claims | 2 RED boundaries; 36 passed in 22.08 seconds |
| Worker startup, periodic reconciliation, cancellation and legacy delivery | `test_run_finalisation.py test_worker_recovery.py test_worker_attempts.py test_run_recovery.py` | 8 new RED regressions; 88 passed in 113.31 seconds, including over-100 startup pagination and exactly-once CRDT/history writeback |
| Recovery and finalisation coverage at `92d4b1a` | Same 88-test target under command-local coverage | 88 passed in 122.63 seconds; recovery 99%, finalisation 91%, combined 97% |
| Dependency lock/export consistency | `python scripts/export_requirements.py --check` with pinned uv 0.12.15 | Passed |
| Compilation and side-effect-light imports | `compileall`, entrypoint `py_compile`, web/worker/collab import smoke | Passed |
| Full integrated PostgreSQL regression at `4f03f63` | `python -m pytest -q -p no:cacheprovider --basetemp=.cache/pytest-roadmap-full` | 1233 passed, 4 skipped, 4 existing SQLAlchemy warnings in 191.47 seconds |
| Scoped service coverage at `4f03f63` | Coverage run of the 12 AI/attempt integration targets | 302 passed; 92% total across context, jobs, shared validation, approval, archives, conversion and finalisation (individual modules 88–98%) |
| Frozen/archived project approvals and cross-scenario signal injection | `test_ai_approval.py test_ai_context.py` | 32 new RED regressions; all 162 tests passed in 91.71 seconds |
| Steps-drawer save tracking | Actual drawer callback in frontend harness | 2 RED persistence regressions; shared save path fixes pending and rejected saves |
| Native Univer selection without facade emission | `frontend/tests/univer-selection.test.cjs` and rendered browser | 2 RED regressions; all 4 selection tests passed, including foreign workbook/sheet rejection; real mouse/keyboard selection now enables matrix actions |
| Final full PostgreSQL regression and coverage | `coverage run --source=<nine changed service modules> -m pytest -q --basetemp .cache/pytest-roadmap-final -p no:cacheprovider` | 1342 passed, 4 skipped, 4 existing SQLAlchemy warnings in 380.94 seconds; 93% combined line coverage, individual modules 88–99% |
| Final frontend and offline production delivery | `npm test`, `npm run build` | 79 passed; all three production bundles built; refreshed Univer artifact retained for the native-selection fix, unrelated generated drift restored |
| Final isolated browser acceptance | Real Edge/Playwright UI plus real Flask/PostgreSQL services and synthetic provider | 43 primary assertions plus 21 candidate/review assertions, zero failures or blocked steps; no automatic tasks, run records or execution requests |

RED/GREEN checkpoints are retained in the engineering branch. These focused
results prove the named software boundaries; they are not interchangeable with
a final full-suite, current-head CI or real-target acceptance result.

## Independent review

The bounded read-only review identified four actionable issues: terminal-status
commit racing retest/writeback, stranded RUNNING attempts after interruption,
unbound archive metadata, and oldest-prefix recovery starvation. Metadata
binding, atomic/durable finalisation, interrupted reconciliation and recovery
fairness are implemented with regression evidence. The review used contained
probes, not PostgreSQL or Silver. Its final context/approval/UI pass found frozen
project approvals, cross-scenario signal seeds and untracked steps-drawer saves;
all are fixed with RED/GREEN evidence. Browser acceptance additionally exposed
Univer's facade SelectionChanged not emitting for native selection: the actual
`sheet.operation.set-selections` command is now consumed with workbook/sheet
identity checks. The production bundle and real pointer/keyboard interaction
are verified, not replaced by the built-in grid.

The final agent lanes hit provider/account limits. Their terminal handles were
closed; persisted work was inspected and completed locally without resetting
credentials or repeatedly respawning failed agents. No second agent review of
the final patch is claimed.

## Completion audit

| Milestone | Authoritative engineering evidence |
| --- | --- |
| M1 | Server-owned `_context`, source/dependency versions, real persisted-row selection, shared edit/apply validator, atomic partial approvals, project lock and CRDT guard regressions |
| M2 | Actual exporter/parser conversion, immutable saved-model/input/result archives, trusted DB digest binding, archived failure analysis, atomic history/CRDT finalisation and retest fencing |
| M3 | AI slots 2, chunks 8, retry rounds 3, UUID attempts, cancellation/retry fences, durable circular cursors, startup interruption handling, asynchronous approved-queue publication and exactly-once writeback regressions |
| M4 | Document/source provenance and review paths, SBS revisions remaining non-current after approval, reviewed library rows, browser persistence and zero automatic execution requests |

Detailed browser conditions and limitations:
[browser acceptance](ai-browser-2026-10-02.md).

## Remaining gates

Release integration remains controlled by green PR CI, followed by the main
workflow. Local logs under `.cache/roadmap-final*`, `.cache/frontend-final*`
and `.cache/ai-browser/` preserve exact results and asset hashes. Backend sources
are unchanged since the full run at `cf71e35`; later engineering changes are
the independently tested native-selection adapter and its production artifact.

The real two-module/20-approved-viewpoint pilot, manual timing, acceptance rate,
human intervention count and real Silver/license validation remain separately
human-triggered and unrecorded. No efficiency or real-runtime claim is made.
