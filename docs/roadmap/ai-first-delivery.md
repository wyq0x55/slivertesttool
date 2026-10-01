# AI-first engineering delivery

Source plan: [ai-first-execution.md](ai-first-execution.md).
M0 landed in PR #34 (`dde8513`); both PR and main CI passed.

## Scope and ownership

1. Main lane: server-owned context, existing matrix selection, scheduling,
   cancellation/recovery, documented source provenance and final integration.
2. Draft lane: shared output validation and atomic, version-safe approvals.
3. Evidence lane: actual exporter/runner validation and immutable run archives.

Each lane has a dedicated worktree and PostgreSQL database. The root worktree's
existing local changes remain untouched. Engineering commits and integration
are authorized by the long-goal request; product asset approval and execution
remain explicit human actions.

The evidence and UI agents were interrupted by provider quota/HTTP 429 errors.
Their persisted work was inspected and integrated locally, not retried blindly.
Independent review subsequently resumed; a new isolated recovery lane addresses
the review findings while browser acceptance runs against a separate mock server.

## Canonical context contract

`docs/contracts/ai-context.schema.json` defines the server-owned `_context`
inside `AiDraft.input_json`. The server never accepts `_context` from clients.
Procedure requests use `payload.item_ids` and optional `payload.model_id`.
Legacy `viewpoints[].item_id` requests are resolved to the same stored rows;
client titles, versions and registries are not the source of project truth.
The generated `viewpoints[]` entries contain `ref`, `item_id`, `version` and
the actual viewpoint fields. Library inputs use `procedures[].item_id/version`.
Model context contains saved-model identity and SBS content hash. Provenance
identifies source type, identity and content digest; client document/code
excerpts are explicitly submitted evidence, not trusted project metadata.

`runtime_inputs` contains server-snapshotted constant values and full library
definitions, not just names. Client-supplied runtime definitions are discarded.
All generated, manually edited and selected-for-approval procedures are built
by the production exporter and parsed into the vendored runner's actual Step
objects. Conversion problems become targeted generation retry feedback.

Approval compares the stored generation version to a locked live row. Missing
snapshots require regeneration rather than silently using the current version.
Partial approval is one terminal decision with explicit applied/skipped refs;
unknown/duplicate/empty selections are rejected. No approval starts a run.

Shared validator boundary:
`validate_output(scenario, payload, output, *, refs=None, for_apply=False)`
returns a list of problems. Generation and manual edit use it; approval uses
`for_apply=True`, rejecting unresolved execution inputs in selected entries.
Atomic approval has exactly one commit after all selected writes and review
metadata succeed. Existing CRDT and service permission guards still apply.

Failure requests require `item_id`, `task_key`, and optional positive integer
`run_count`. The task must belong to the project and the approved archive must
identify that exact row. Inputs, logs, model identity and artifact hashes come
from sealed attempt evidence, never current mutable steps or a submitted log.
Review rechecks the archive digest. Legacy runs without approved row snapshots
cannot be promoted to this evidence-backed analysis contract.

## Immutable execution and finalisation

Each human submission pins inputs and saved SIL/DLL/SBS/PDB dependencies under
`<project workspace>/.evidence/<task_key>/<run_count>/`. Retests and separate
tasks never share result directories. The existing explicit bootstrap owns
the new `run_evidence` table and nullable history `run_count` migration.
Manifest and outcome SHA-256 digests are bound to the database attempt identity;
rewriting an archive and its embedded hashes is rejected. A failed submission
transaction may leave an uncommitted directory; a new explicit submission
preserves it under an aborted-attempt name without overwriting committed history.

The runner validates pinned documents before acquiring its execution-file claim.
Results are sealed on success, error and cancellation. Pooled console logs are
sliced before sealing. Log reads share one aggregate byte budget, and numeric
expression operations have finite/size limits before potentially costly native
arithmetic. Mock outcomes are explicitly `synthetic`, not real Silver evidence.

Sealing persists a trusted outcome and durable writeback intent. Finalisation
then locks the task/attempt and commits terminal status, history, row fields,
CRDT writeback and the finalised marker together. A failed writeback leaves the
sealed outcome available for idempotent recovery. An old completion cannot
write a verdict into a retest's attempt or overwrite its state.

Recovery persists circular keyset cursors independently of attempt callbacks,
so pending, invalid or locked old tasks cannot starve later approved submissions.
Periodic scans reconcile trusted sealed RUNNING outcomes without backend
execution and preserve live unsealed attempts. Before consumer startup, a
finite number of bounded pages visits the existing RUNNING set: unsealed pinned
interruptions become ERROR with manual retry required. Cancellation requests do
not strand already-running attempts. Legacy untyped queue messages identify
only initial attempt 1, never the current retest. A corrupt hash-bound outcome
is reported without promoting success or silently executing another run.

## Execution status

- M1: context, native matrix selection, inline validation and atomic partial approval verified; frozen/archived projects and cross-scenario input injection fail closed.
- M2: actual conversion, pinned evidence, archived analysis and atomic finalisation verified, including exactly-once CRDT/history reconciliation.
- M3: AI concurrency 2, chunks 8, rounds 3, cancellation/retry, cursor fairness, startup reconciliation and asynchronous approved submission recovery verified.
- M4: documented viewpoints, non-current candidate SBS and reusable-library human review paths verified. Real pilot evidence remains a rollout gate.

The final local gate is 1342 backend tests, 79 frontend tests, all three
production builds and 64 isolated browser assertions. The nine changed backend
service modules have 93% combined line coverage. The final native-selection
repair includes its refreshed Univer artifact; other build drift is excluded.
Exact results and integration boundaries are recorded in
[engineering verification](../verification/ai-first-engineering-2026-10-01.md).

## Acceptance boundaries

Synthetic tests prove software boundaries, not real Silver/license behavior.
The real two-module/20-viewpoint pilot and manual timing remain unrecorded.
Efficiency claims and upstream rollout depend on that human-triggered pilot.
