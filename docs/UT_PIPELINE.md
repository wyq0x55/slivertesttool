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

`budget_stop_observations`, when declared, lets a batch stop a function early
instead of paying to the ceiling: ut-agent asks its own deterministic forecast at
every obligation boundary, and once that many boundaries agree the projection no
longer fits, the function yields. Empty keeps the forecast advisory. The count is
a declared input, so the same IR, rules, ceiling and count stop at the same
obligation; the forecast reads measured costs but only decides *when to stop*,
never what an expectation is. A stopped function keeps its finished cases and
hands the unstarted obligations to review exactly like a budget stop, and the
batch continues with its other functions.

Stopping an active job no longer waits for the current function to finish: the
worker writes `<batch>/stop.request`, which `ut-agent bridge generate --stop-file`
reads at the next obligation boundary. Generation ends there, finished cases stay
and the rest become review work. The stop is recorded as `OPERATOR_STOP:`, never
as `GENERATION_BUDGET_EXHAUSTED:` - no budget measurement was made, so triage
must not read an operator's decision as evidence that the ceiling is too low. The
file is removed when the attempt ends, on stop and on resume, so it can never
stop the next run.

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

## Budget suggestion

`GET /budget/suggestion` (read-only, `project.view`) turns the project's own
finished batches into advice for the next batch's `function_step_limit`. The
rule is the one the ceiling was always derived from: ceiling ≈
steps-per-obligation coefficient × obligation count. It reads no new data -
each finished batch froze its declared ceiling in `config_snapshot`, and each
job froze the steps it consumed in its generation receipt plus the obligation
count and exhaustion flag in its witness handoff.

One sample is one function of one finished batch; `generating` and `running`
batches are skipped because their outcome still moves, and samples never cross
projects. Only functions that did **not** exhaust their budget enter the
coefficient: an exhausted function's `steps_used` stopped at the ceiling, so
counting it would pull the coefficient below the evidence. Those samples are
still counted in the exhaustion rate and reported with their stop positions.
The coefficient is
the **median** of `steps_used / obligations`, not the mean, because one
repair-heavy function would otherwise decide the value for all of them.

The suggestion is `ceil(median × target obligations × safety factor)`, at
least 1. The safety factor defaults to 1.5 (`budget.DEFAULT_SAFETY_FACTOR`):
the median describes functions that already ran, and a new one may sit above
the middle of that sample. The target obligation count defaults to the median
obligation count of the samples and can be overridden with
`?target_obligations=`. Below two finished batches or three non-exhausted
functions the response is `state=INSUFFICIENT_SAMPLES` with what is missing,
never a number; the operator keeps choosing the ceiling by hand until then.

The response lists the samples it used, their batch IDs, the median, the
safety factor, the target, the suggestion and a `sample_digest` (SHA-256 over
the sorted samples), so the same records always produce the same number and
can be re-derived later. Nothing is written back: no batch's snapshot is
changed, no history is rewritten, and generation never reads the coefficient.
The binding page shows the number next to the ceiling field and the
"fill" button only copies it into the form - saving that form is what makes it
reach batches created afterwards.

## Derived batches and comparison

`POST /batches/<id>/derive` queues a new batch for the functions whose witness
run confirmed entries, and never writes the source batch: its frozen config,
provenance, status and step ceiling stay as delivered. The new batch records
`derived_from_batch_id` and freezes the config current at derivation, so a
`function_step_limit` in the body changes only what batches created afterwards
see. Without an explicit `targets` list it takes the source jobs whose handoff
is `HANDOFF_READY` or `NO_ADMISSIBLE_OBLIGATION` and whose confirmation is
`CONFIRMED`, one target per function; a function with no confirmed entry is not
regenerated, and an empty selection is refused.

`GET /batches/<id>/compare?against=<id>` compares the two batches per function -
the frozen ceiling, the generation-reported ceiling, the obligation totals, the
completed and remaining counts, where generation stopped, the assertion count
the suite recorded, and the replay verdict and confirmation. It stores nothing
and changes nothing, and a figure the source never recorded comes back `null`
rather than a guessed zero. Calibration of the per-obligation step coefficient
reads these records and does not write back to a delivered batch.

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
