# Experimental Clang host and stub candidates

The UT project binding selects `execution_backend=winams` (default) or `clang`.
The normal batch/job/attempt workflow and human review gate own both paths.
Host PASS means host observation comparison passed. Raw host coverage is displayed
as measured; it does not assert Baseline coverage acceptance, target execution,
or formal delivery validation. Human approval never changes the machine verdict
or those evidence qualifications.

## Diagnostic guidance

Task details render checker guidance as a processing entry point, ordered next
steps and a verification condition. Each unresolved obligation shows its target,
solver state, original reason, constraints and individual guidance. Host coverage
issues show the available decision locations and associated cases. Full received
issue JSON remains expandable, including evidence longer than the compact view.
Historical reports without guidance explicitly say that guidance is unavailable.

The diagnostic download button creates a local `ut-platform.diagnostic-packet/1`
JSON file for the displayed attempt, including its CSV hash, target, check,
execution summary and an investigation instruction for a local model. Downloading
does not call an AI provider or submit a revision. The packet preserves upstream
detail limits and does not contain every source artifact. Local-model repair
prompts also include the checker's guidance for open missing-case obligations
(`ut-repair/7.5`); evidence gaps and unconfirmed infeasibility are not row work.

The batch API includes selected frozen constraint fields so the details page
can show the test requirements selected for that batch. Checker reports also
carry the generation-bound policy and map each gap to its requirement and exact
target. Disabled selections remain visible as not required; MC/DC grouping is
marked as presentation only. Missing reference annotations name the label and
count to restore and are distinct from missing cases. Group labels are not
assigned to a single input row. Legacy batches publish their Baseline and
coverage selection without presenting derived policy switches as user choices.
Configuration-conflict gaps are excluded from the machine row-work allowlist.
The local diagnostic
packet includes the frozen batch selection alongside the displayed check.

## Project settings

`GET/PUT /api/v1/projects/<project_id>/ut/config` uses the existing project view /
project edit permissions and CSRF contract. All settings are frozen in
`UtBatch.config_snapshot` when queued; changes affect new batches only.

| Field | Default | Validation |
| --- | --- | --- |
| execution_backend | winams | winams or clang |
| stub_mode | deterministic | deterministic or llm; llm requires clang |
| stub_model | empty | String override on the existing global AI gateway |
| stub_max_rounds | 2 | Integer 1..5; independent of CSV repair rounds |
| host_clang | clang | Nonempty executable name/path |
| host_llvm_cov | llvm-cov | Nonempty executable name/path |
| host_llvm_profdata | llvm-profdata | Nonempty executable name/path |
| host_profile_runtime | empty | Optional runtime library path |

Manifest, index and Product root are required for both backends. WinAMS also
requires its work root; its stub-build selection and existing CSV repair stay
unchanged. The UI hides WinAMS-only fields for clang and disables mutation for
read-only users. AI endpoint and secret remain in the global AI settings.

## Ownership and execution

- Models and additive PostgreSQL column upgrades: `app/models/ut_pipeline.py`
  and `app/bootstrap.py`; bootstrap remains the sole schema owner.
- Dispatch, snapshots, gates, stop/retry and review: `pipeline.py`.
- Subprocess transport: `bridge.py`, contract `ut-agent.bridge/1`.
- Model calls and artifact persistence: `host.py`; it never interprets C semantics.
- Typed inputs, trusted harness, body validation, CSV binding, compile/run and
  host checks: external ut-agent `host-prepare` / `host-execute`.

The clang path requires generator IR, intents, original reference CSV and the
current candidate. Only the external bridge may prove that candidate rows still
map to intents. Unmapped edits block with useful host diagnostics; the platform
does not replay reference inputs as if they were the edited candidate. The host
branch consumes the returned check directly, without WinAMS checks, expected
value capture backfill or generic CSV model repair. The reference itself must
match the bridge's deterministic projection of the validated Suite, including
all declared output expectations; changing both CSV files cannot bypass that
binding. Raw overrides without typed replay mappings are unsupported. Suite-level
review warnings and validation errors remain visible or block execution.
Intent reconstruction retains semantic truth vectors, call invocations, stub
behavior and raw fields, so reference reprojection uses the same typed evidence
as formal generation. Missing selected generation units fail with the original
extractor/generation report diagnostic instead of an empty successful response.

Deterministic mode calls host-execute once. LLM mode prepares once per execution
attempt, skips the model when no stubs exist, and otherwise calls the existing
provider with the prepared messages, temperature zero and at most 8192 output
tokens. The model proposes only per-callee body statements, without outer braces.
It cannot replace the trusted harness, subject, observations or expected values.

The exact proposal keys are `status`, `stub_bodies`, `evidence_ids`, `limitations`.
CANDIDATE requires both `typed-function-ir-v5` and `suite-stub-projection-v1` IDs.
UNKNOWN / UNSUPPORTED / NEEDS_REVIEW require empty bodies and nonempty limitations;
they go directly to the human gate. Malformed replies or failed host candidates
may retry within the frozen stub round budget. Feedback includes bounded compiler
diagnostics, mismatching cells and differences from deterministic observations.
`HOST_UNSUPPORTED` is retryable only for `HOST_STUB_BODY_INVALID`, so a rejected
body can be corrected within that same budget. CSV/intent binding failures and
other unsupported results still go directly to a human gate.

Each round retains prompt digest, raw reply, request-bound proposal envelope,
model/usage, compile/run artifacts and check paths in
`UtAttempt.execution.stub_generation`. Provider failure preserves partial evidence
and uses the existing model-unavailable gate. Stop finishes the current call and
retains evidence before further work. Every LLM retry gets a new attempt ID,
including a preparation failure before evidence exists and unchanged CSV, preserving previous artifacts and the
`expected_attempt_id` stale-review fence.

## Review and downloads

Job detail shows host-only evidence, line/branch/MC/DC measurements and per-stub
round results. Missing metrics stay unmeasured. Authenticated project viewers can
download recorded artifacts through
`GET .../jobs/<job_id>/attempts/<attempt_id>/artifacts/<artifact_id>`.
The service resolves IDs only within the owning job directory and verifies file
hashes. Requests never supply filesystem paths. Host approved-export JSON and CSV
entries retain host-only evidence qualifiers; approval does not install stubs into
WinAMS or mutate formal generation outputs.

## Verification

Run targeted PostgreSQL tests with an explicitly disposable `TEST_DATABASE_URL`
whose database ends in `_test` (the fixture resets its tables):

```text
python -m pytest tests/test_ut_host.py tests/test_ut_pipeline.py tests/test_ut_job_lifecycle.py tests/test_ut_gate_recovery.py tests/test_ut_export.py tests/test_bootstrap_lifecycle.py -q
node --test frontend/tests/ut-binding.test.cjs frontend/tests/ut-project.test.cjs
```

`tests/test_ut_host_subprocess.py` is opt-in via `UT_HOST_TEST_AGENT_ROOT` and
`UT_HOST_TEST_PYTHON`; set `UT_CLANG_EXTRACT` if the external checkout has no local
extractor build. These tests use the real bridge/extractor/LLVM and fake model
replies. No test starts or contacts an actual model gateway. Production builds
use `npm ci` then `npm run build` in `frontend/`.

With those variables and `UT_CLANG_EXTRACT` set, also run:

```text
python -m pytest tests/test_ut_host_workflow.py tests/test_ut_host.py tests/test_ut_host_subprocess.py tests/test_ut_export.py -q
```

The workflow tests use real HTTP handlers, PostgreSQL, Huey immediate-mode task
entry, index extraction, formal generation, Clang, llvm-cov, artifact downloads,
human approval and export. Three repository-owned C functions exercise a branch,
a scalar dependency and a void dependency in both stub modes. Missing LLVM tools
and extraction failures retain actionable diagnostics. These tests do not prove
asynchronous worker scheduling, real customer corpus acceptance or local-model
quality.
