# Efficiency pilot engineering readiness

Source plan: [pilot readiness](../roadmap/efficiency-pilot-readiness.md), following
PR #37 at `97ba659b7b1ec3b4f66f86aba5637903633a0058`.
Final locally verified implementation: `0de25fd`.
Operator instructions: [efficiency pilot](../efficiency-pilot.md).
Local engineering verification is complete. Exact-candidate and resulting main
CI remain the authoritative integration gates.

## Delivered guarantees

| Criterion | Guarantee | Executed target |
| --- | --- | --- |
| AC-001 | One strict typed input pins two distinct modules, twenty distinct viewpoints, original saved-model digest, revisions and operator declarations; invalid identities, extra fields and non-finite measurements fail closed | `tests/test_pilot_contract.py`, malformed-input CLI cases |
| AC-002 | Live collection requires an explicit PostgreSQL host/database and project-view actor, owns a fresh repeatable-read/read-only transaction, rejects dirty sessions and releases its connection | PostgreSQL collector permission, denied-write, concurrent-commit and cleanup cases; CLI subprocess preflight |
| AC-003 | Only selected drafts/attempts count; archive checks reuse the database-bound verifier and compare actual approved procedure/constants/library snapshots; simulation, tampering, unrelated history and ambiguous links do not complete assisted coverage | `tests/test_pilot_audit.py`, historical-link reducer cases |
| AC-004 | Missing timings remain null; measured zero is retained; rates use unique candidates; repeated attempts cannot invent twenty completed viewpoints; negative savings are allowed and rollout always requires the owner | `tests/test_pilot_metrics.py` |
| AC-005 | Offline is the default, callers cannot inject observations, diagnostics exclude credentials/raw input, provenance is counters-only and receipts come only from the actual captured generation boundary | CLI subprocess/error cases, provider worker/route and receipt-trust cases |

The input contract derives JSON Schema directly from `PilotInput`. Document
revision and viewpoint approval references are operator declarations;
`TestItemRow.review_status` approves a verdict, not pilot viewpoint selection.
Current readiness and immutable historical metrics are deliberately separate.

## Executed RED/GREEN checkpoints

| Behavior | RED evidence on the delivery branch | GREEN evidence |
| --- | --- | --- |
| Strict pilot contract | `b592900`: 28 executed failures for the absent contract; the original commit body mistakenly says 29, but the retained log and subsequent GREEN body record 28 | `35c5d76`: 28 pass |
| Evidence-bound reducer | `4bcdfed`: 34 executed failures for the absent reducer | `0b99ea9`: 62 contract/reducer cases pass |
| Actual approved-generation execution link | `8426cab`: one historical manual run incorrectly completes assisted measurement | `b4edb19`, `9c4cc47`: typed link plus 63 contract/reducer cases pass |
| Offline-safe CLI | `69ecb6f`: 19 executed missing-command failures | `fd42fe5`: 82 contract/reducer/CLI cases pass |
| Live CLI and authentic history statistics | `3087c0e`, `c1dfe19`: each has one executed failure | Collector integration and `0db9013`: both pass in the 215-case integrated target |
| Generation provider capture | `d2a0d1e`, originating at `72869e9`: missing capture is exercised before implementation | `6c207e3`, originating at `de2ee96`; worker/route/thread and retry tests pass |
| Empty HTTP return mixed with a stub | `226b006`, originating at `30337d2`: empty returned calls allow false API-only classification | `104353a`, originating at `fb0c2c1`: every non-throwing return is counted; the lane's exact 39 cases pass |
| Read-only collector and archived-document link | `6a66e47`, `ddcff1c`, `b255426`: corrected harness executes missing-feature and causal-link failures | `d955c6f`, originating at `e6a67cd`: lane's 71 cases pass |
| Normalized library names preserve pinned raw-library identity | `fc85869`, originating at `88e5527`: one of two isolated causal-link cases fails; the lane records one failed and 79 passed | `88419c4`, originating at `f3cd910`: all 80 collector cases pass in integrated verification |
| No implicit/default database target | `1e23d71`: nine intercepted URL cases reach application creation instead of being rejected | `3e7a548`: the same nine cases pass before factory creation |
| Supplied/stale receipts are not observed provider calls | `e3e9d58`: two isolated PostgreSQL cases fail without any provider call | `ea2eebd`: the same two pass; unrelated metadata and caller input are preserved |
| Approval precedes immutable input pinning | `7075e8b`: seven failed and one passed; twenty authenticated pre-approval archives incorrectly complete assisted measurement | `0de25fd`: the same eight cases pass; unknown/naive pin timestamps and reversed ordering remain unlinked, aware offsets normalize to UTC, genuine runtime counts remain visible |

All checkpoint commits are reachable on the delivery branch. Raw logs remain
in `.cache/pilot` in the parent and isolated lane worktrees. This table preserves
the guarantees if the integration commit is squashed.

## Harness failures excluded from proof

The audit lane initially hardcoded its local database, producing 71 setup errors
in the parent integration target. The provenance fixture also replaced supplied
DSNs with a lane literal. Both now honor explicit `TEST_DATABASE_URL`; audit
model imports are aliased instead of changing the production model globally.
These setup failures are not production-logic RED evidence.

Two parent test processes then overlapped against the same disposable database,
causing schema-reset interference. The affected `library-red.log` and
`core-provider-green.log` are invalid gates and are retained for diagnosis.
The isolated library reproducer, receipt reproducer and single integrated
215-case run replace them. A capacity-failed metrics agent made no edits;
the parent implemented that lane without changing models or blindly retrying.
The first ordering target read UTF-8 manifests using the Windows locale and had
seven encoding/setup failures plus the real twenty-archive regression. Only
the corrected explicit UTF-8 target (seven logic failures, one passed) is
counted as the ordering RED gate.

## Verification environment and checks

CPython 3.10.18 uses the existing workspace environment. Tests target the owned
PostgreSQL 18 cluster on loopback port 55435; parent and development agents use
different disposable databases. `RUNNER_BACKEND=mock` and `HUEY_IMMEDIATE=1` are
explicit. No production database, Silver process or external/paid AI is used.
Synthetic archive fixtures with runtime-shaped metadata test authentication
logic; they are not proof of actual licensed Silver execution.

| Gate | Observed result |
| --- | --- |
| Integrated contract/reducer/CLI/collector/provenance target | 215 passed in 51.85 seconds; no skipped tests or warnings |
| Interim full regression before the final ordering repair | 1648 passed, 4 skipped, 4 existing warnings in 499.01 seconds; 90% combined line/branch coverage across the eight modules; not the final source gate |
| Focused new-module coverage | Contract 100%, reducer 98%, collector 88%, CLI 91%, including branches; all-eight-module focused aggregate 76% is interim, not the full regression coverage gate |
| Frontend delivery regressions | 79 passed, zero skipped |
| Three production bundles | `npm run build` succeeds; generated-only bundle drift is restored, no frontend bytes are delivered |
| Frozen dependency export | `python scripts/export_requirements.py --check` passes using command-local uv 0.12.15 |
| Compilation/import smoke | `compileall app scripts`, entrypoint `py_compile`, web/worker/collab/pilot imports pass with an unreachable database |
| Diff | `git diff --check` passes |
| Final stable-source full regression | 1656 passed, 4 skipped, 4 existing SQLAlchemy warnings in 483.18 seconds |
| Final eight-module coverage | 90% combined line/branch coverage: contract 100%, reducer 98%, collector 89%, CLI 91%, provider 90%, base 88%, scenarios 82%, jobs 96% |
| Standalone lint/type checking | Not configured locally; no success claimed |

The dependency graph is unchanged. Prior delivery recorded 98 advisories in the
existing frontend lock; this slice neither upgrades that graph nor claims a
fresh clean dependency-security scan.

## Independent review

The provenance implementer independently reviewed the parent contract/reducer/
CLI, running 44 pure cases and nine metric follow-ups. Its explicit-target P2
finding is fixed with nine RED/GREEN cases. Collector review used 35 runtime
cases and 18 interaction cases on its own disposable database, identifying a
pre-approval archive P2: content equality alone could promote older archives.
Eight ordering regressions fix it. The independent exact-`0de25fd` recheck
preserves twenty authenticated pre-approval attempts with zero assisted links,
an incomplete measurement and null savings; its post-approval control links
all twenty. Eight temporal and eighteen adjacency cases pass, with no remaining
scoped P1/P2 blocker. Logs remain in the provenance lane's `.cache/pilot`:
`independent-collector-temporal-recheck-0de25fd.log` and
`independent-collector-temporal-tests-0de25fd.log`.

The collector implementer independently reviewed provenance boundaries (26
mock-only checks), identified lane-DSN and supplied-receipt findings, then
verified the parent fix at `88419c4` using eight offline tests in 0.010 seconds.
It reports no remaining receipt/DSN finding; that check is not full runtime
acceptance. The parent also reviews all integrated production diffs.

## Remaining owner boundary

The real project, two modules, twenty approved viewpoints and manual timing are
not supplied or invented. This tool neither selects them nor approves assets,
activates SBS, calls a provider or submits a run. Historical missing receipts
remain unknown; current model settings cannot backfill provenance. API-path
counters do not prove a vendor's internal model behavior. `rollout_approved`
remains false even for a complete operator-recorded measurement.

The original checkout remains at `79df201` with its user-owned `CONTEXT.md` and
ADR 0003; their status and SHA-256 hashes match the protected initial capture.
The owned test cluster is retained and will be stopped after final gates. The
completed manifest and proprietary assets belong in private local storage, not
this repository.

## Delivery self-check

| Axis | Score | Evidence and improvement boundary |
| --- | --- | --- |
| Accuracy | 4/5 | Executed checkpoints and corrected harness failures are distinguished; no vendor runtime, independently timed baseline or clean dependency scan is claimed. Keep those owner-specific facts separate from engineering gates |
| Completeness | 4/5 | Contract, permissions, readonly/consistent transactions, receipt origin, pinned documents, ordering and statistical edge cases have tests. Historical missing receipts cannot be reconstructed by this slice |
| Clarity | 4/5 | One operator guide documents fields, exit codes and approval boundaries. Full raw logs still require the retained local worktrees |
| Actionability | 4/5 | Schema/template commands and explicit audit instructions are executable. Actual project IDs, approved viewpoints and measured timing remain private owner inputs |
| Conciseness | 4/5 | Tables preserve squash-safe guarantees without transcript dumps. This engineering audit is intentionally longer than the user-facing handoff |

Overall: 4.0/5; no critical scoped self-check issue. Improvements are to retain
owner runtime/timing evidence separately and supplement historical unknown
provenance only through a newly authorized, observed generation, never a
retroactive inferred receipt. The assessment matches the bounded preparation
request rather than claiming that the real business pilot has run.
