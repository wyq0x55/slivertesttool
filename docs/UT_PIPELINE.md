# Unit-test pipeline (ut-agent + Clang/LLVM + local model)

Clang/LLVM is the platform's sole production UT execution path. A project binds
its ProjectManifest, function index and source root; selected functions enter:

```text
index → one shared Clang extraction → Typed FunctionIR → deterministic Suite/CSV
→ each completed function immediately enters LLVM execution, coverage and check
→ identified missing-case work: model inputs → static typed witness validation
→ typed expected values → immutable HOST_DERIVED child → execute its new SHA
→ machine result → human review
```

The original compiler, OMF conversion, linked WinAMS builds and WinAMS execution
are not scheduled, including on retry, resume or worker recovery. Missing LLVM
tools or unsupported semantics produce actionable blocking results, without an
alternative executor. Existing test jobs are not migrated or reinterpreted.

## Ownership and evidence

ut-agent owns extraction, deterministic generation, typed bindings, harnesses,
implementation-derived expected values, execution and measured coverage. It runs
in its separately versioned checkout/interpreter through `ut-agent.bridge/1`;
this application never imports it. The platform owns configuration, jobs,
immutable candidate versions, bounded model requests and human decisions.

The primary execution command is `bridge execute` with IR, intents, manifest,
reference and candidate inputs. `bridge host-prepare` prepares typed stub requests;
`bridge check` verifies proposed rows before execution and does not run WinAMS.
The model only proposes input rows or per-callee stub bodies. It cannot rewrite
the subject, original Suite, existing expected values or trusted harness.

Actual execution retains `HOST_EXECUTION` as its source identifier. Formal LLVM
workflow PASS and human approval do not manufacture physical target execution
evidence. UNKNOWN, UNSUPPORTED, policy conflicts, hash drift and required
coverage gaps remain explicit and do not become PASS by changing the executor.

## Configuration

Environment settings are `UT_AGENT_ROOT`, `UT_AGENT_PYTHON`,
`UT_AGENT_EXTRACTOR`, `UT_GENERATE_TIMEOUT`, `UT_EXECUTE_TIMEOUT`,
`UT_BRIDGE_TIMEOUT`, `UT_REPAIR_MAX_ROUNDS` and `UT_REPAIR_TIMEOUT`.
The model endpoint and credentials use the existing global AI settings.

The project configuration requires only the manifest, index and Product/source
root. Both paths are on the execution server; the binding page can type them or
pick them through `GET /ut/server-paths` (project editors only), which lists
directory and CSV names one directory at a time and never file contents.
`execution_backend` is not a setting: the server records `clang` for
every configuration and job, and ignores a submitted value. Configure `host_clang`,
`host_llvm_cov`, `host_llvm_profdata` and, if the declared ABI requires it,
`host_profile_runtime`. ut-agent checks the required tool/runtime identities.
An ABI/layout profile is semantic input, not a request to run the original
compiler. The manifest's CSV projection profile remains an exchange-format
contract and does not require a WinAMS installation.

Stub mode is deterministic or local-model candidates. `stub_model` and
`stub_max_rounds` control stub proposals; `repair_model` and
`max_repair_rounds` independently control missing input cases. Both are available
on the LLVM settings page.

`function_step_limit`, when declared, is the per-function semantic step ceiling
handed to `ut-agent bridge generate --function-step-limit`. It is estimated from
the number of input obligations, never from a measured share of one run, so the
same inputs keep producing the same deliverable. Empty is not zero: the flag is
omitted and ut-agent keeps its own ceiling of 250,000,000 steps. A function that
exhausts its budget stops with the cases it completed and the rest of the batch
continues. ut-agent builds that predate the flag reject the launch before any
work starts, so the affected jobs are blocked as an environment problem naming
the version requirement, not reported as a generation failure.

A batch freezes the effective configuration and
versioned requirements when queued. Changing `function_step_limit` therefore
affects only batches created afterwards; a queued, running or delivered batch
keeps the value it froze.

## Budget exhaustion and witness handoff

ut-agent's generation report records, for each function, whether its unfinished
obligations can be handed to witness work: `HANDOFF_READY`,
`NO_ADMISSIBLE_OBLIGATION` or `NO_SUITE`, with the budget counters, obligations
by cause, admissible obligation IDs and unfinished MC/DC pairs. Each time
generation runs, including a retry, silver reads
`<generation>/index-generation-report.json` once per job and freezes that record
into the job's own `witness` metadata (`silver.ut-witness/1`). The record is an
observation of generation, not a job state: it does not gate the batch, and it
never feeds back into generation, expected values or the witness ledger.

A function the report cannot describe - the report is missing, unfinished, lacks
that function, or reports a state this version does not know - is frozen as
`UNKNOWN` with the reason, never as "nothing to do". `NO_SUITE` is listed
separately as missing triage input: the function exhausted its budget before any
suite existed, or its process timed out, so there is nothing for witness triage
to read. Only a `HANDOFF_READY` function offers the triage action.

Triage (`POST /jobs/<id>/witness/triage`) runs `witness triage` over the
generation inputs silver already stored and records the counts it returned. It
calls no model. Proposal (`POST /jobs/<id>/witness/propose`) runs
`witness propose` against the configured loopback model endpoint and is reached
only from that operator action; nothing in the batch lifecycle schedules it. It
requires a finished `PROPOSABLE` triage and writes candidates only - no expected
values, no manifest or ledger write, no job state change. Each run writes a new
directory under the batch's `witness/job<job_id>/`, never reusing an existing
one - `run<N>/triage` for triage, `run<N>/propose<N>` for proposal, so a second
proposal keeps the earlier candidates - and a failed run is recorded with its
message rather than left running. The model key reaches the child in ut-agent's
own environment variable, never on a command line.

Confirmation (`POST /jobs/<id>/witness/confirm`, body `{"write": false}`) replays
the proposal's candidates with `witness confirm`, which compiles each candidate
and runs its single-case harness through the host toolchain **this batch froze**
(`host_clang`, `host_llvm_cov`, `host_llvm_profdata`, `host_profile_runtime`).
It adds no toolchain and no second execution environment. It needs a finished
proposal with `UNCONFIRMED_CANDIDATES` and reuses that proposal's `run<N>`, so
its evidence lands beside the candidates in `run<N>/confirm`. With
`--write` it also merges the confirmed entries into the project ledger, but only
when the ProjectManifest itself declares `witness_ledger.path`, has not declared
`delivered`, and no other job of the project is writing the ledger at the same
time; silver refuses those cases before anything compiles when the declared
manifest path is absolute, and a relative one is left to ut-agent, which refuses
the same cases itself. The platform exposes no `witness synthesize` entry, so a
candidate can only come from an operator's proposal.
Whatever happens, the record keeps `evidence_source=HOST_EXECUTION`,
`counts_as_target_evidence=false` and `delivery_validated=false`: host execution
observes a harness, it is not target or delivery evidence. Confirmation does not
change the job's verdict, the batch status or the candidates, and a ledger it
writes binds only the *next* batch whose generation reads it - the current batch
keeps the conclusion it already reached.

## Job lifecycle

Each attempt (`GENERATOR`, `LLM`, `HUMAN`, `HOST_DERIVED`) retains its exact CSV,
SHA, check and execution evidence. Appended blank outputs may only be filled by
the proven typed implementation oracle. The resulting child must run with its
own SHA before approval. Expected-value disagreements in original rows require
review; no execution-capture overwrite path exists in platform jobs.

Every machine outcome ends at a human gate. Approving a non-PASS candidate
requires a review note; approval does not change its machine verdict. Stop waits
for the current external call, preserving results. Retry preserves candidate
lineage and model budgets; an unavailable model consumes no repair round.
`HOST_TOOL_UNAVAILABLE` without another error is a retryable `blocked` environment
gate. Restoring the tool at its frozen path permits the same job to execute
again. Changing configured tool paths requires a new batch; retry does not
replace the existing snapshot. Semantic, policy and binding failures remain
human work, including when a tool failure is reported alongside them.

Shared extraction is serialized, while finalized functions can execute on other
Huey worker threads before sibling generation completes. At least two worker
threads are needed for asynchronous overlap. Per-step timing separates waits,
actual requests and shared/nested intervals. The details page also provides
measured C0/C1/MC/DC and per-line evidence with unknowns left unmeasured.

Review stores a candidate-bound `review_trace` in the job generation metadata
before changing review state or invalidating machine evidence. The snapshot
retains the attempt ID, CSV SHA-256, machine verdict, gate code and reason,
gap counts, expected-value lineage and reviewer decision. Human approval does
not erase the machine gate. An incomplete report keeps the open gap count
unknown rather than reporting zero.

Approved CSV exports include these facts in both `manifest.json` and
`manifest.csv`, together with `stub_mode` and the post-review verdict.
`trace_status=CAPTURED_BEFORE_REVIEW` identifies a matching saved snapshot;
historical approvals without one use `CURRENT_EVIDENCE_ONLY`. That label
does not claim to reconstruct evidence available at the time of approval.

Silver calls run through an owned process with one absolute monotonic deadline
covering launch, model configuration, execution and IPC transfer. Timeout or
cancellation terminates the owned process tree. Cleanup failures retain
ownership for retry and keep pool capacity reserved until termination is
confirmed. ut-agent subprocesses use the same containment boundary, including
descendants that inherit stdout/stderr pipes.

## Verification

Run focused PostgreSQL lifecycle/configuration tests and frontend regressions,
then the repository CI contract. Actual extractor/LLVM integration tests use
repository-owned synthetic C, typed appended inputs and scripted model replies.
Negative tests prohibit original-compiler/OMF/WinAMS calls and check missing
tools, unsupported ABI, evidence drift, policy requirements and model outages.
No old-job migration or database cleanup is part of this change.
