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

## Execution status

- M1: in progress; isolated implementation lanes dispatched next.
- M2: in progress; archive and exporter validation lane dispatched next.
- M3: pending M1 integration; concurrency 2, chunk size 8, rounds 3.
- M4: pending execution loop; proposals retain source provenance and require review.

## Acceptance boundaries

Synthetic tests prove software boundaries, not real Silver/license behavior.
The real two-module/20-viewpoint pilot and manual timing remain unrecorded.
Efficiency claims and upstream rollout depend on that human-triggered pilot.
