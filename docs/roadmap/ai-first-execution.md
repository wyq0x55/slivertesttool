# AI-first execution roadmap

Approved direction: efficiency first, existing viewpoints first, one production
worker process, and parallel development with explicitly separated write scopes.
The dates below are planning windows, not delivery guarantees.

## Locked boundaries

- A single production worker may execute concurrent Silver tests within the
  administrator's license limit. Development agents are not runtime workers.
- Imported remote models remain locally saved copies. Registration must never
  overwrite another saved copy, including after a version relabel.
- AI produces drafts. Humans approve formal assets and explicitly start runs.
- Preserve project permissions, optimistic versions, CRDT single-writer
  ownership, and explicit application bootstrap ownership.
- Mock tests, synthetic report downloads, and historical acceptance do not prove
  a new real Silver run. Do not repeat generic legacy cleanup without evidence.

## Milestones and exit gates

| Milestone | Planning window | Executable scope | Exit gate |
| --- | --- | --- | --- |
| M0: deliver the integration baseline | October 1-7, 2026 | Review the 17 existing integration commits; fix blocking defects; run isolated PostgreSQL, production build, and browser acceptance; preserve existing local changes | Review and validation evidence recorded, commit approved, PR integrated. The project owner selects two real modules and 20 already-approved viewpoints and records manual workflow timing before efficiency claims |
| M1: generate drafts from existing viewpoints | October 8-November 15, 2026 | Select matrix rows; assemble project context server-side; generate procedure drafts; support partial human approval | Permissions and source versions enforced; manual edits and apply use the same validator; approval is atomic; stale drafts cannot silently overwrite rows. Existing assets remain runnable without AI |
| M2: connect validation to execution evidence | November 16-December 31, 2026 | Validate actual runner conversion, parameter values and timing; explicitly approve execution; retain immutable run artifacts; classify failures | Each run identifies its saved model, approved input snapshot and artifact archive. Runner conversion failures block execution. Real Silver evidence is separately verified, not inferred from mocks |
| M3: bounded scheduling and recovery | January 1-February 15, 2027 | Bound AI concurrency to 2, chunks to 8, and validation rounds to 3; recover interrupted generation and submission attempts; preserve one production worker | License capacity and shutdown drain remain separate. Recovery cannot replay a human execution decision or mutate another attempt. Cancellation, exhaustion and interrupted-process behavior have regressions |
| M4: upstream authoring and reuse | February 16-March 31, 2027 | Introduce documented viewpoints, candidate SBS and proposed reusable libraries only after the execution loop is reliable | Source provenance and review status remain explicit; proposals require approval; measured pilot evidence justifies expansion |

## Implementation order

1. Finish M0 review fixes and commit/PR gates. Do not merge an unvalidated baseline
   simply because its original suite was green.
2. Start M1 with draft/apply contract corrections: consistent generation-base
   versions, shared validation, one transaction, and library parameter encoding
   compatible with the existing runner exporter.
3. Add the existing-viewpoint selection entry and server-owned project context,
   then parallelize independent UI and generation work around that contract.
4. Prove the approved-draft-to-runner boundary before adding recovery or wider
   source ingestion. Keep real Silver acceptance explicitly human-triggered.
5. Measure completion time, human intervention count, draft acceptance rate and
   executable conversion success on the approved pilot. No invented baseline or
   synthetic sample is an efficiency claim.

## Current delivery state

M0 fixes and isolated acceptance landed in PR #34 at `dde8513`, with green PR
and main CI. See
[`../verification/m0-delivery-2026-10-01.md`](../verification/m0-delivery-2026-10-01.md).
The user authorized autonomous M1-M4 engineering delivery, including commits,
pushes and green-CI-only integration. M1-M4 landed in PR #35 at `c7ee963`,
with successful PR and main CI. The follow-up real-Silver release landed in
PR #36 at `cc56b20`, with successful PR and main CI. Its reproduced defects,
real runtime evidence and remaining boundaries are recorded in
[Silver release verification](../verification/silver-release-2026-10-02.md).
GitHub PR and main checks remain the authoritative integration record.
The existing root worktree changes to `CONTEXT.md` and ADR 0003 are preserved.

Implementation and exact test checkpoints are recorded in
[engineering delivery](ai-first-delivery.md) and
[engineering verification](../verification/ai-first-engineering-2026-10-01.md).
The original milestone windows are not a runtime or efficiency proof.

The real two-module/20-approved-viewpoint pilot and manual timing remain
human-owned rollout gates. Formal asset approval, candidate SBS activation and
real Silver execution are never inferred from engineering completion. The
isolated historical Silver acceptance is not the two-module efficiency pilot.

## Post-release integrity lane

The next autonomous engineering slice is a bounded audit of the delivered
execution contracts, not a new efficiency-pilot feature or generic refactor.
Reproduced findings cover PostgreSQL session-time drift, pooled shutdown drain,
AI recovery liveness/publication and trusted model reuse on retest. Each repair
requires isolated RED/GREEN evidence, an independent review, full regression
and exact-head CI before integration. See
[post-release verification](../verification/post-release-integrity-2026-10-02.md).
The original checkout, production database and prior Silver evidence remain
untouched; formal approval and real pilot inputs remain human-owned.
