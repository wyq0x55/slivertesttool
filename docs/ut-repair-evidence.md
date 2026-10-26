# Unit-test repair evidence and prefix reuse

Repair prompt version `ut-repair/7.0` separates the baseline instruction prefix
(shared across functions) from the function prefix and changing round state.
The function prefix contains exact column tokens, ranges and typed obligation
context. Repeated column-name prefixes are factored by exact string
concatenation; no identifiers or input positions are inferred or renamed.

For identified `ut-agent.repair-context/2` evidence with a complete input-scope
attestation, the prompt lists only the
exact input tokens bound to the left operand, dynamic right operand and dynamic
mask of unresolved obligations and their parent paths. It preserves original
`I<n>` IDs. Hidden input values are copied from the reference row and the append
validator rejects attempts to modify them, including by full column name.
Legacy/unidentified contexts, unbound operands, and functions whose calls,
value origins or write effects can feed state replay keep the full column view;
they do not establish inferred driver mappings. Scoped
row rendering lists only the editable inputs while preserving reference row
numbers. The complete input contract and semantic evidence remain in the
snapshot digest even when their rendered view is smaller.

The snapshot is persisted as `UtJob.generation.repair_prompt_prefix` and reused
after worker restarts. Same-version changes to the baseline, columns or typed
context stop at the human gate. Prefix hashes and character counts are stored
on each revision. A separate digest covers the complete semantic evidence even
when some records do not fit the rendered prefix. A prompt-version change creates a new snapshot and naturally
invalidates the old cache prefix.

Dynamic context follows the static prefix: open gaps, complete reference rows
within a 12,000-character budget, bounded issue and coverage records, execution
summary and the last three revision histories. Original reference row numbers
are retained. Essential context is not cut to fit: requests above the total
96,000-character guard stop before contacting the model, do not consume a model
round and require human handling. This is a character guard, not a tokenizer's
exact token/context-window measurement.

The repair client requests SSE and usage counters. It records first event,
first text, elapsed time, input/output tokens and cached input tokens when the
gateway supplies them. Missing cache usage is not proof of a miss. Interrupted,
truncated or failed streams never become accepted model replies. Local/intranet
requests continue to bypass the system proxy. SSE can avoid a long silent
response transfer but cannot guarantee that an upstream service emits tokens
or heartbeats during prefill, stays online or keeps its KV cache resident.

WinAMS execution now records hashes of the function's coverage report and
supporting line/address/jump/binary artifacts. Before passing observations to
the model, the checker validates the candidate hash against the execution and
capture receipts and checks the source-qualified function identity,
checks artifact paths/digests and reparses the report. It passes report
positions, observed C1 fields and execution counts to the witness report and
repair prompt. Source text is not parsed for C semantics.

New isolated WinAMS runs enable the existing `[TEST].PutLineNo` setting, when
present and supported, and retain its before/after hashes. The coverage parser
preserves the optional source-line column and the documented `T/`, `/F` and `/`
observations, including explicitly unexecuted rows. A source line and a missing
outcome are still observations; they do not establish the IR mapping below.

The observed legacy reports do not establish an IR branch ID, C true/false
orientation, per-row attribution, or an assembly-to-C mapping. Those mappings
remain `UNKNOWN`. Coverage observations never close a typed input obligation,
establish MC/DC pairing or approve a candidate on their own. Missing artifacts,
changed hashes and percentage-only evidence cannot turn into branch proofs.

WinAMS remains serial and every outcome still requires human review. None of
these transport or cache changes proves requirement-correct expected values,
eliminates HTTP 499/502, or establishes real repair effectiveness without a
corresponding real execution trial.

## Human navigation and offline diagnostic packets

The check panel paginates the full `completeness.unresolved` and
`completeness.output_pending` worklists. An issue's `detail` is only a bounded
preview. A reported count that differs from its supplied list, or an explicit
`details_complete=false`, requires a fresh complete check and routes the job to
`needs_human` before another repair request. This also protects historical
reports from checkers that serialized only the first 50 records.

Typed branch locations, ancestor outcomes, operands and exact input-column
tokens are shown only with the supported witness contract, identified evidence
and matching candidate/check/witness hashes. Opening the related inputs marks
the proven columns across existing CSV rows. A missing case has no existing row
location. Missing bindings remain `UNKNOWN` or `NO_DIRECT_INPUT`.

`GET /api/v1/projects/<project_id>/ut/jobs/<job_id>/attempts/<attempt_id>/csv?format=diagnostic`
uses the existing authenticated attempt-download permission and returns
`ut-platform.diagnostic-packet/2`. It contains the selected attempt's version and
SHA-256, all input vectors with original 1-based reference-row numbers, exact
headers and reported domains, the complete check, execution evidence, frozen
configuration/provenance and an append-only JSON reply contract. Historical
exports use only that attempt's lineage through its sequence number. A changed
CSV hash or malformed rows that the repair parser would discard prevent export;
they cannot be advertised as complete inputs.

The packet omits CSV labels and expected-value cells from its input vectors and
does not bundle source files. Replies can propose appended input rows within
the existing verifier's obligation and column scope, or decline with missing
evidence. The packet does not automatically import or apply a local AI reply.
Execution, recheck and human review still follow. Selection changes, detail
refreshes and version mismatches invalidate pending browser downloads.

## Evidence gates for configured checks

Configuration conflicts are not input-row work. A requirement whose
`configuration_status` is `NEEDS_REVIEW`, including one outside the bounded
issue preview, routes the job to `CONFIGURATION_CONFLICT` / `needs_human`.
An absent, empty, truncated or count-inconsistent completeness report routes a
potential PASS or repairable FAIL to `REPORT_INCOMPLETE`. A prior execution
environment failure retains its actionable blocking reason. Neither gate calls the repair model or consumes a repair
round. Existing locked Baseline and explicit-policy records remain supported;
a missing completeness report is not treated as an empty complete report.

Every supported report supplies an `unresolved` array and an explicit
`unresolved_count`. Counts must be non-negative integers, excluding Booleans,
and must equal the supplied array length. Each record must be an object with
a unique, nonblank string `obligation_id`; an optional `requirement` must be an
object. A supplied `details_complete` must be a Boolean and must be true.
Field-level validation errors identify what the checker must regenerate.
Known legacy reports containing only the unresolved list/count pair remain
supported. If either `output_pending` or `output_pending_count` appears, both
are required and obey the same list, count and record rules. A flag alone,
a count alone, malformed records or partially supplied pending-output fields
cannot authorize machine PASS, another repair round or offline repair scope.
Host preparation failures can legitimately precede a completeness report. The
Host workflow preserves their execution reason while still enforcing supplied
identity conflicts; missing worklists cannot hide why preparation was blocked.

Before repair, result persistence, Host PASS handling and final approval, the
platform re-reads the selected attempt CSV and compares its digest with the
attempt SHA and every supplied identity. This includes
`candidate_sha256`, `candidate_witnesses.candidate_sha256`,
`execution.candidate_sha256`, and the existing Host
`execution.input_binding.candidate.sha256`. A disagreement produces
`INTEGRITY_ERROR`; the candidate is saved as `BLOCKED`, witness authority is
withdrawn, and no model call or repair round occurs. A checker that omits an
optional report digest is not rejected solely for that omission: available
identities are still checked and the report records
`candidate_identity.status=UNKNOWN` with
`reason="Checker omitted its candidate digest; available execution digests were compared, not inferred"`.
Host evidence retains `counts_as_target_evidence=false`.

### Host appended-input repair

The clang backend may enter input-row repair only for open `MISSING_CASE`
obligations in a complete report with identified, candidate-bound witness and
input-column evidence. Unsupported coverage, preparation/runtime failures,
configuration conflicts and unknown bindings remain human work. The frozen
`repair_model` and `max_repair_rounds` control input proposals; `stub_model` and
`stub_max_rounds` independently control Host stub proposals. An unavailable
repair provider consumes no input round; retry resumes that stage without
renewing the stub evidence. A stub retry still creates a new attempt so old
review selections remain stale.

Repair prompt version `ut-repair/7.6` distinguishes scalar input constraints
from `predicate_context`. Raw decision atoms describe the typed decision;
they are not a conjunction that every proposed input must make true. An exact
boundary with no required outcome does not acquire one from its decision text.
Streamed provider failures retain bounded upstream type/code/message details
with configured keys and Bearer credentials redacted, and remain failures.

Appended rows pass the existing static `check`/witness verifier before LLVM
execution. This check runs without an execution argument and never launches
WinAMS. Host prepare/execute receive the configured manifest for typed binding;
the original Suite and reference CSV remain immutable.

The Host engine may return `execution.typed_candidate` with contract
`ut-agent.host-typed-candidate/1`, an absolute path, artifact SHA,
`source_candidate_sha256`, `reference_rows`, and
`expected_value_source=IMPLEMENTATION_DERIVED`. The platform verifies those
identities, confines the file to the execution sandbox/root, rejects lossy CSV
records and checks unchanged columns, input vectors, row counts, reference rows
and existing expected cells. Only blank output cells after `reference_rows`
may be filled. Valid bytes become a child `HOST_DERIVED` attempt with the receipt
and `oracle_source=IMPLEMENTATION_DERIVED`; they never replace their parent.
The child must execute again with its new SHA before it can be approved, even
with an explicit note. No Host result enters WinAMS capture, and Host evidence
retains `counts_as_target_evidence=false` and `delivery_validated=false`.

The integrity failure record contains
`evidence_integrity: {status: "UNKNOWN", reason: ...}` and an error issue with
code `INTEGRITY_ERROR`. These fields preserve the received evidence without
promoting it to validated target evidence. They are not a new database schema
or HTTP contract.

Historical persisted reports are rechecked when approval is attempted. A missing
or changed CSV cannot be approved, including with a note: restore the original
version file or submit a new revision and recheck it. For an intact version file,
supplied identity conflicts, incomplete reports or frozen configuration conflicts
require a human note even if the job was already marked `awaiting_review`. The
note records the human override decision; it does not turn `UNKNOWN` into
`VALIDATED`, restore witness authority, or make Host evidence target validation.

## Per-function generation handoff

A batch keeps one shared Clang extraction. With a matching ut-agent version,
`bridge generate --unit-ready-dir` publishes authoritative
`ut-agent.generated-unit/1` receipts as each function finishes, separate from
best-effort progress snapshots. The platform checks artifact paths, required
CSV/IR/Suite/identity-summary files and their hashes, then checks the exact CSV
bytes again when saving the immutable initial attempt. Batch constraints and
provenance are committed before that function is dispatched for execution.

The batch remains `generating` while undispatched functions are being produced;
individual jobs can already be executing, repairing or awaiting review. The
final response reconciles existing receipts without creating another attempt,
re-enqueueing a completed job or overwriting a stop/review/supersede decision.
A later generation failure marks the batch failed and only still-generating
jobs as failed; already dispatched jobs retain their candidates and continue.
The failed batch status describes generation, not every individual job result.

Execution overlaps generation when a Huey worker is available (at least two
worker threads are needed for overlap in normal asynchronous operation).
The generation slot is acquired without blocking another worker; waiting batch
tasks reschedule, and startup recovery includes both queued and scheduled tasks.
The existing WinAMS execution lock and human approval gates still apply. An old
ut-agent that rejects the new flag fails explicitly instead of silently changing
the generation/execution protocol. Existing artifact/version locks are preserved.
