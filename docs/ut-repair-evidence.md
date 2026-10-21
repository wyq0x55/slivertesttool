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
