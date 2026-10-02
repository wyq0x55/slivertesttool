# Efficiency pilot readiness

Source: [AI-first execution](ai-first-execution.md), implementation-order step 5.
Baseline: `97ba659`, PR #37. This slice prepares a real owner-selected pilot;
it does not select the project, approve viewpoints, activate SBS or run Silver/AI.

## Delivery sequence

1. Freeze a strict typed input and observation contract.
2. Develop the read-only database/archive collector and metric reducer in
   isolated worktrees with disjoint write surfaces.
3. Connect an offline-by-default CLI, template/schema output and explicit
   opt-in PostgreSQL audit. No implicit production database fallback.
4. Run isolated PostgreSQL, CLI end-to-end, regression/coverage and independent
   security review, preserving executed RED/GREEN checkpoints.
5. Commit/push and integrate only after exact-candidate and main CI succeed.

## Acceptance criteria

| ID | Observable guarantee | Safe verification |
| --- | --- | --- |
| AC-001 | Exactly two distinct modules and twenty distinct viewpoint IDs, with pinned row/document/model revisions and operator approval references; booleans, duplicates, unknown fields and non-finite timing are rejected | Pure typed-contract regressions and CLI malformed-input cases |
| AC-002 | A live audit requires an explicit database target and an authorized project-view actor; a fresh repeatable-read PostgreSQL transaction rejects writes; inputs are never approved or queued | Owned PostgreSQL 18 on loopback 55435, separate disposable databases, denied-write and unchanged-row assertions |
| AC-003 | Only explicitly listed project-scoped drafts/attempts are inspected; run artifacts use the existing database-bound digest checker and link to the approved generated execution documents; simulation, unrelated history, missing outcomes, wrong identities and tampering stay distinguishable | Seeded archived evidence and draft fixtures, never vendor runtime or external providers |
| AC-004 | Missing manual baseline/intervention data remains missing; reducer counts unique procedure candidates and real authenticated attempts, distinguishes readiness from measurement and always requires owner rollout review | Pure reducer edge cases and serialized collector/reducer integration |
| AC-005 | The command works without database/network access by default, never accepts caller-injected trusted observations, prints no credentials/raw documents/logs, and has actionable exit codes | Subprocess CLI end-to-end with poisoned default database/provider configuration |

## Contract and semantics

`app.services.pilot_contract.PilotInput` is the authoritative typed contract for
all Python participants. Its JSON Schema is derived, never hand-maintained.
Operator timings and document/viewpoint approval references are declarations,
not database-certified facts. `TestItemRow.review_status` approves a verdict;
it must not be repurposed as proof that a viewpoint was approved for this pilot.

Current row/model readiness and immutable historical attempt evidence are
reported separately: ordinary draft application and execution writeback can
advance the live row version without invalidating a trusted prior archive.
No metric is inferred from task creation/review timestamps. Human intervention
minutes are operator-reported effort, not assumed equal to wall-clock time.
Neither a calculated percentage nor a synthetic fixture grants rollout approval.

Discovery refinement: existing draft metadata stores a model label and usage,
not the origin of each returned provider call. A disjoint additive instrumentation
lane captures counters at the existing generation boundary; historical/missing,
replaced-generator and mixed API/stub origins remain unknown. Counters contain
no request text, endpoint, credential or response body, and recording them never
starts a provider call. A complete measurement also requires a causal link from
each retained execution document to a selected approved/generated procedure;
a valid historical manual run on the same row/model alone is insufficient.

## Ownership and boundaries

- Parent: typed contract, CLI, docs, integration and final gates.
- Audit lane: `app/services/pilot_audit.py`, `tests/test_pilot_audit.py`.
- Metrics lane: failed before edits because the selected model was at capacity;
  the parent owns `app/services/pilot_metrics.py`, `tests/test_pilot_metrics.py`.
- Provenance lane: existing AI provider/generator/job call boundaries and
  `tests/test_ai_provider_provenance.py`, no overlap with audit or CLI files.

The original checkout's `CONTEXT.md` and ADR 0003, production data and retained
Silver evidence remain untouched. The owner still needs to supply the actual
project, modules, twenty reviewed viewpoint identities/revisions and manual
timings. This preparation deliverable is not completion of that business gate.

Local implementation and independent review are complete at `0de25fd`:
1656 backend cases pass (four existing skips/warnings), 79 frontend cases pass,
all three production bundles build and the eight changed/new modules have
90% combined line/branch coverage. See
[verification evidence](../verification/efficiency-pilot-readiness-2026-10-02.md).
Exact-candidate and main CI remain the integration gates.
