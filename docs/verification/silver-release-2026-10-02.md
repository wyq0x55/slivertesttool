# Real Silver release verification

## Scope and provenance

The M1-M4 engineering baseline landed in PR #35 at
`c7ee9637b1b25ecefd3ade51495c81e5a697a3f6`, with successful PR and main CI.
This follow-up exercises that baseline against the actual Silver runtime and
fixes only reproduced submission, saved-model and immutable-evidence defects.
It does not measure AI efficiency or approve product assets.

The root checkout was not updated: it remains at
`79df20153a2631daa0d724def5186f1a52ed2feb`. Its modified `CONTEXT.md` and untracked
ADR 0003 remain user-owned. Engineering runs in the isolated worktree
`C:\workspeace\slivertesttool-wt-silver-acceptance` on
`codex/real-silver-acceptance`.

Read-only inspection of the configured source database found zero projects,
models, matrix items, tasks and run records, and one user. No cause is inferred.
Its settings remain `license_limit=2`, `license_inuse=0`, `license_draining=0`.
A current custom-format backup was preserved before preparing the isolated lane.

| Input | Provenance |
| --- | --- |
| Current read-only database backup | `.cache/silver-release/current-source-readonly.dump`; SHA-256 `9a76ea2eaac7a74e7e06982112f97bfd387c3f6f4326c0bff191067edbc0b76d` |
| Historical acceptance database backup | `silver_test_platform-20260924-133723-before-e05cd08.dump`; SHA-256 `a108d980bf76bed89e65c9778529f290f0b9286b7a397bd9b79c5ebca798d9d2` |
| Original saved model | `instance/model/RWS/isuzu/isuzu.sil`; SHA-256 `59fe5b03f0764ba74a060a3a8e7b43c6c7b79485b8734c09952073948133893d` |
| Model identity | `isuzu@rws_development-Suzuki_dev-4fb2bd97726` |
| Real lane | Owned PostgreSQL 18, loopback port `55433`, database `stp_real_silver_20261002`; copied model assets and separately rooted workspaces/pool/logs |
| Regression lane | Separate `stp_real_silver_full_20261002` and `stp_real_silver_targeted_20261002` databases; never run pytest against the real lane |

Historical Huey queue/schedule data was excluded from restoration. Three stale
historical tasks were quarantined only in the clone. Explicit bootstrap migrated
that clone; production data/schema and original SIL/DLL/SBS/PDB assets were not
written. The preflight and final audit record their hashes and source counts.

## Reproduced defects and TDD evidence

| Guarantee | RED checkpoint and actual failure | GREEN checkpoint and proof |
| --- | --- | --- |
| Existing imported integral step numbers remain runnable | `314fff1`: four integral-float validation regressions fail | `546a301`: 37 export tests pass; booleans, fractions, non-finite values and duplicate numeric identities still fail closed |
| XML tags are not part of dependency filenames | `5a7d11e`: native XML dependency pinning fails on `<sil-line>` prefix | `58d99b6`: quoted/unquoted module variants pass; later structural fixes retain this regression |
| Embedded Python DLL references are not model dependencies | `ad2dcc3`: four pinning/registration regressions fail on script paths and malformed XML | `2affd3e`: 78 focused tests pass; only actual `sil-line` elements are rewritten |
| Native XML lexical structure is retained and malformed registration allocates nothing | `7ae2bae`: four valid lexical regressions fail; `1bb0bff`: corrected filesystem test reproduces an orphan directory | `a847c70`: 80 focused tests pass; Expat spans preserve declarations, comments, entities, CDATA scripts, UTF-8 text and quoted `>` attributes outside rewritten module content |
| Native CSV teardown cannot mutate sealed artifacts | `022910d`: two normal/error pooled-writer regressions fail hash validation after a late flush | `d00c845`: 53 evidence/finalisation/pool tests pass; live outputs remain in pool-owned `.results`, checked copies enter the immutable archive only once |
| LF and CRLF model bytes survive registration and pinning | `62e9dda`: Windows byte assertions yield three failures and one already-passing CRLF-registration case | `0dad8f5`: 104 focused tests pass; saved configurations are decoded/read and encoded/written without newline translation |

The first orphan-test attempt used the wrong fixture config interface. Its
setup failure is not counted as valid RED evidence; the corrected test executed
registration and proved the orphan before the fix was restored.

All checkpoints belong to this branch and remain available after integration.
This table preserves the proof if GitHub squash-merges the delivery.

## Real runtime results

Final runtime acceptance at `0dad8f5`: **119 assertions passed, five real PASS
attempts**. `acceptance.json` SHA-256:
`5a2a9227cde0cc99714456528486ced731c10508d4e46283fabb5eef7d277712`.

| Phase | Attempt identities | Verdict |
| --- | --- | --- |
| Two authenticated submissions | `T000783/12`, `T000784/12` | PASS / PASS |
| Two explicit retests | `T000783/13`, `T000784/13` | PASS / PASS |
| Explicit run after worker restart | `T000783/14` | PASS |

Both owned service generations, 15 and 16, exited normally. Each retained
`total=1`, released to `in_use=0`, and entered transient draining. Restart
cleared draining. All five complete artifact sets still pass hash validation
after final shutdown. Final XML has 1,156 CRLF sequences and zero doubled CRs.

The harness starts actual `run.py`, authenticates with an isolated operator and
session/CSRF token, and submits only APL-001002 and APL-001003. Preflight records
that these two historical rows were reviewed; APL-001005 was pending and was not
selected. Retesting is explicitly authorized execution, not automatic approval.
Normal verdict writeback correctly returns the selected rows to pending review;
the harness never changes that decision.

Each final attempt must satisfy all of these gates:

1. Actual Silver creation (`mock=False`), pooled reuse, native XML acceptance
   and real `host.dll` loading, not just a process-start or mock label.
2. Terminal `PASS`, a finalised database-bound attempt, exactly one model-stamped
   history record, and committed matrix verdict/model/log writeback.
3. Retained `Console.log`, `jdgrslt.log` and `output.csv`, plus authenticated
   report-ZIP download and validated artifact hashes.
4. Two explicit retests preserve prior archives; worker restart permits one
   explicit new attempt; all five archives validate again after final shutdown.
5. One worker process with four consumer threads respects the existing single
   Silver license. Both shutdowns drain/release it without overwriting the
   administrator limit; restart clears transient draining.

Collaboration is disabled in this isolated HTTP/runtime lane. The database-first
matrix write and durable mirror queue are checked, but this run does not claim
a live collaborative editor consumed that queue. CRDT consumption and exactly-once
history/writeback remain covered by the PostgreSQL finalisation regressions.
With one licensed slot these five real runs are serialized; four worker threads
are not evidence of two simultaneous licensed Silver instances.

Diagnostic attempts are preserved rather than relabelled: the earlier XML
serializer was rejected by Silver, leaving no real host module and producing
genuine `FAIL` verdicts. Later genuine `PASS` runs exposed native CSV writes after
sealing. The corrected lane uses separate mutable output paths; expected values,
case procedures and original proprietary model bytes were never changed to make
the tests pass.

Two harness-only assumptions were corrected without platform changes: `result`
is a first-class row field rather than a `custom_values` entry, and new verdicts
legitimately request human review. Vendor licensing/A2L/stub-variable/charset
warnings remain in real logs; a case PASS is not proof that every optional
feature, model variable or historical-schema timestamp is correct.

## Final engineering gates

| Gate | Actual result |
| --- | --- |
| Final source commit | `0dad8f5`; later edits are documentation only |
| Focused current-source regression | 104 passed in 35.92 seconds |
| Full isolated PostgreSQL regression and changed-module coverage | 1373 passed, 4 skipped, 4 existing SQLAlchemy warnings in 358.44 seconds; 81% combined line coverage across the four changed modules (runner 78%, model service 73%, evidence 94%, validation 91%) |
| Frontend delivery regressions | 79 passed; command-local `NODE_PATH` uses the existing read-only TypeScript installation; no frontend sources or bundles changed |
| Compilation and imports | `compileall app`, entrypoint `py_compile`, web/worker/collab import smoke and `git diff --check` pass |
| Dependency export / frontend production build | Required exact-head GitHub CI gates; local export check was blocked by installed uv 0.12.16 versus required 0.12.15, not by dependency edits |
| Lint / standalone type checker | Not configured/installed locally; no lint/typecheck success is claimed |
| Independent bounded review | XML/helper and staged-output ownership reviewed; malformed-registration orphan and CRLF writer findings addressed with executed RED/GREEN tests |

Backend command: CPython 3.10.18, `RUNNER_BACKEND=mock`, `HUEY_IMMEDIATE=1`,
an explicit isolated test secret and separate test/queue database URLs, then
`coverage run --source=app.services.run_validation_service,app.services.run_evidence_service,app.services.project_model_service,app.runners.test_runner -m pytest -q --basetemp=.cache/pytest-silver-release-final-bytes -p no:cacheprovider`.
Coverage tooling is command-local; production dependencies are unchanged.

PR and resulting main CI are authoritative integration gates. They must finish
successfully at the exact candidate revision before this work is considered
landed; local mocks and historical results cannot substitute for that check.

## Retained evidence and remaining rollout boundary

Artifacts live under the engineering worktree's ignored `.cache/silver-release/`:
`preflight.json`, `acceptance.json`, `final-audit.json`, `acceptance-final.console.log`,
`release-final-bytes-backend.log`, `release-final-bytes-coverage.txt`,
`server-15.*.log`, `server-16.*.log` and
`runtime/workspaces/RWS/.evidence/<task_key>/<run_count>/`. Previous failed
acceptance JSON and RED/GREEN logs are retained separately. Proprietary assets,
database backups and runtime credentials are not part of the Git patch.

The owned web/worker/Silver processes and PostgreSQL cluster are stopped after
validation, without a machine-wide process sweep or deleting retained evidence.
The source database and root user changes remain untouched.

The two-module/20-approved-viewpoint efficiency pilot is still human-owned,
not a completed gate. Its next input is a real project selection, two module
identities and twenty already-approved viewpoint IDs with document/model
revisions. Record manual baseline minutes, assisted total minutes, human
intervention minutes/count, accepted draft count and executable conversion/run
outcomes against the retained attempt IDs. No timing, acceptance rate, formal
approval or candidate SBS activation is inferred from these two historical cases.

## Delivery self-check

| Axis | Score | Evidence and boundary |
| --- | --- | --- |
| Accuracy | 4/5 | Runtime verdicts and failing diagnostics are distinguished; optional model/vendor and historical-schema observations are not independently accepted |
| Completeness | 4/5 | Submission, retest, archives, history, writeback and lifecycle are exercised; human efficiency/approval gates deliberately remain separate |
| Clarity | 4/5 | Checkpoint and gate tables are explicit; raw artifacts still require the retained local worktree |
| Actionability | 4/5 | Exact commands, source commits and artifact roots are recorded; a new live pilot needs real project/module selection |
| Conciseness | 4/5 | Tables preserve squash-safe evidence; the final user handoff is intentionally shorter than this audit |

Overall: 4.0/5. No critical self-check issue remains in the scoped engineering
delivery. The next improvement is a measured owner-selected pilot, not another
generic refactor. This assessment preserves the user's runtime/approval boundaries.
