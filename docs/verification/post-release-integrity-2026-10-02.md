# Post-release execution integrity

Status: implementation and integration gates in progress.
Source plan: [AI-first execution](../roadmap/ai-first-execution.md).
Baseline: PR #36, `cc56b20356ead4acbed889e16b5539dc81eb590c`.

## Scope and acceptance

This is a bounded follow-up to shipped M1-M4 execution contracts. It does not
introduce an efficiency pilot, a new worker process or generic legacy cleanup.
The parent lane handles UTC connections, worker acquisition and stale dispatch;
separate AI and evidence worktrees handle recovery and retest authentication.

| Criterion | Observable guarantee | Verification |
| --- | --- | --- |
| AC-001 | Non-UTC server/driver settings cannot shift stored task/event timestamps, account lock deadlines, import expiry or presence freshness; other driver options and stored instants are preserved | PostgreSQL time-contract tests in Shanghai and New York sessions, HTTP login, rollback/reconnect and legacy `timestamptz` reads |
| AC-002 | Shutdown drain never raises pool capacity or starts a new borrowed attempt; release never decrements another attempt's count; existing runs can finish | PostgreSQL worker/pool tests, drain-before-claim/waiting races, same-process concurrency and failure cleanup |
| AC-003 | Recovery preserves an owned live AI call, does not flood publication, retries failed publication and rejects superseded messages before slot rescheduling | Controlled provider/clock and fenced queue regressions; no external AI calls |
| AC-004 | Retest authenticates the previous model archive against its original database-bound attempt before creating a new trusted snapshot | Coordinated artifact/embedded-hash tampering, missing identity and valid-retention regressions |
| AC-005 | Repairs preserve public approval/run contracts, bootstrap ownership, dependencies, root user changes and prior Silver evidence | Full PostgreSQL regression, frontend delivery/build, unreachable-DB import smoke, independent review and exact-head CI |

## Safety and compatibility

- All databases belong to the new owned PostgreSQL 18 cluster, bound to loopback
  port `55434`. Parent, worker, AI and evidence lanes use different disposable
  databases. No pytest command targets a real Silver or production database.
- The original `C:\workspeace\slivertesttool` checkout stays at `79df201` with its
  user-owned `CONTEXT.md` and ADR 0003. Earlier real Silver assets and evidence
  are neither rewritten nor used as new acceptance results.
- UTC is a per-physical-connection startup policy, registered without connecting
  in `create_app()`. It preserves existing driver options, survives rollback and
  applies only to PostgreSQL engines. No DDL or historical timestamp rewrite is
  introduced. `LM_DISPLAY_TZ` continues to control presentation only.
- A previously shifted naive timestamp cannot be distinguished from a valid
  historical value without provenance. Backfilled offsets or schema changes
  therefore remain a separate backed-up, owner-approved operation.
- One production worker, AI concurrency two and explicit human approval/run
  decisions remain unchanged. Synthetic concurrency is not evidence of two
  licensed real Silver instances.

## RED/GREEN checkpoints

| Repair | Executed RED | Executed GREEN |
| --- | --- | --- |
| UTC application connections | `0f2eb30`: nine failed and one passed; Shanghai locks last too long, New York locks expire immediately | `6fb9bc8`: 34 targeted tests pass, including unchanged factory and isolated SQLite contracts; `app.extensions` has 100% line coverage |
| Worker drain and stale dispatch | `eb04782`: eight failed and two passed, including target restoration, borrow/claim race, count theft and stale scheduling | `5cd9296`: 35 worker/pool/AI tests pass |
| Retest model authentication | `d30c5af`: ten failed and two passed; coordinated manifest/model changes or a missing trusted binding are accepted by the old retest path | `956f27a`: all twelve retest regressions pass; the delegated evidence/report/finalisation target has 63 passed |
| Slow AI ownership and publication recovery | `6d395c5`: nine failed and three passed, including a successful slow provider being superseded and duplicate publication | `2de9c26`: all twelve regressions pass; the delegated adjacent AI target has 168 passed |
| All real AI producers use the lease | `e515321`, `2e4ee89`, `23b13bd`: direct calls, repeated recovery and HTTP creation each reproduce duplicate delivery | `8567fab`: 57 integrated producer/worker/AI tests pass; creation and retry share the production publisher |
| Pool release visibility and temporary denial | `5793cb7`: two regressions fail; an awakened borrower can see unreleased DB capacity and a current delivery can be lost | `3f62cd7`: 73 integrated boundary tests pass; release order and fenced delayed retry are verified |
| Pre-claim infrastructure failure ownership | `5c9d257`, `6c941c8`, `1f15935`: scheduler, acquisition and dedicated-claim errors incorrectly terminalise an unowned attempt | `9a4da02`: 38 worker/claim/live-AI tests pass; both execution modes write runner failure only after successful claim ownership |

The first supplemental concurrency harness released its running pair before
the rejected consumers finished, so later calls could enter a broken barrier.
That harness error is not counted as production RED evidence. The corrected
harness waits for the denied consumers before releasing the executing pair.
The first HTTP-creation test expected 200 rather than the existing 201 contract.
After correcting that harness expectation, it executed the workflow and
reproduced two deliveries. Only the corrected failure is counted as RED.
RED/GREEN commits remain on their development branches; this table preserves
the proof across squash integration.

## Executed checks

| Gate | Observed result |
| --- | --- |
| Interim UTC full regression, before integrating the other repair lanes | 1387 passed, 4 skipped, 4 existing SQLAlchemy warnings in 388.41 seconds; not the final integrated source gate |
| Supplemental worker/recovery target | 122 passed in 150.39 seconds; worker module 71% line coverage, with unchanged maintenance/entrypoint branches outside that focused target |
| Final producer and capacity boundaries | 73 passed in 46.98 seconds |
| Post-review claim/failure target | 38 passed in 29.96 seconds |
| Frontend delivery regressions | 79 passed; no skipped tests |
| Three production bundles | `npm run build` succeeds; unrelated generated-byte drift is restored, no bundle changes are delivered |
| Frozen dependency export | Pass with a command-local uv 0.12.15; production dependency files are unchanged |
| Compilation and imports | `compileall app`, entrypoint `py_compile`, and web/worker/collab imports pass with an unreachable database |
| Lint and standalone types | Not configured locally; no success is claimed |

Frontend installation reports 98 existing dependency advisories (one moderate,
97 high) in the unchanged lock graph. This patch neither upgrades the pinned
Univer stack nor claims a clean dependency-security scan. Advisory applicability
and a compatible upgrade are a separate work item, not a silently widened fix.

## Runtime ownership

Pooled execution uses the same atomic database acquisition as dedicated runs,
in addition to the pool's native-instance bound. A rejected acquisition cannot
decrement another run's counter. Owned capacity is released before an instance
becomes idle; a transient denial retains the same task/attempt in a delayed
delivery, while drain/cancellation/stale-attempt guards prevent new execution.
Retry publication errors retain the queued intent. Infrastructure failure
before a successful claim cannot mark an unowned task failed in either mode.

AI recovery recognizes only local live-thread slot ownership, scoped to engine,
draft and fenced attempt. Slot release and dead-thread reaping clear ownership;
startup remains authoritative before consumers start. A live blocked provider
still consumes its slot until it finishes or its configured timeout expires.
There is no extra heartbeat worker or claim that a hung call can be killed.

`publish_once(draft_id, raw_dispatch)` reserves a 900-second database lease and
passes the pinned attempt to the raw dispatcher. Creation, retry and recovery
all use that boundary. Failure clears only its own publication identity, and
expiry permits at-least-once redelivery; the atomic claim fences execution.
Circular recovery visits at most 200 drafts per scan so leased old prefixes do
not starve later requests. This is not an exactly-once cross-database queue
transaction, and the bounded startup scan is not an all-history immediate SLA.

## Integration and rollout boundary

Final integrated regression, independent review, candidate PR CI and resulting
main CI must finish before this engineering slice is complete. Local logs live
under the parent worktree's ignored `.cache/integrity/`, with separate evidence
under each delegated worktree. The owned cluster is stopped only after all
validation consumers finish; databases and audit artifacts are retained.

The two-module/20-approved-viewpoint pilot and manual timing are still absent.
Project/module/viewpoint selection, formal asset approval, SBS activation and
paid AI/real Silver execution are not inferred from these software tests.
