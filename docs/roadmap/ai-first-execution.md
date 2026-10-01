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

M0 fixes and isolated acceptance are implemented on
`codex/integration-next-phase`, based on `c26976c`. See
[`../verification/m0-delivery-2026-10-01.md`](../verification/m0-delivery-2026-10-01.md).
The user approved committing and pushing the verified patch on October 1, 2026.
PR integration and any main update remain separate delivery gates.
The existing root worktree changes to `CONTEXT.md` and ADR 0003 are preserved.

The following are explicit follow-ups, not completed M0 claims:

- Real pilot module/viewpoint selection and manual timing have not been recorded.
- Report-download rejection still navigates away from the application; matrix
  row links do not yet consume the row UUID for editor focus.
- Caught enqueue failures are retryable; durable crash recovery between database
  commit and queue publication is M3 work, not guaranteed by exception handling.
- Active staging aliases are protected. Immutable per-run archives and full
  isolation of historical artifacts remain M2 work.
