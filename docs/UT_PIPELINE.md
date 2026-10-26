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
root. `execution_backend` is fixed to `clang`; other values and removed
`work_root`/`stub_builds` options are rejected. Configure `host_clang`,
`host_llvm_cov`, `host_llvm_profdata` and, if the declared ABI requires it,
`host_profile_runtime`. ut-agent checks the required tool/runtime identities.
An ABI/layout profile is semantic input, not a request to run the original
compiler. The manifest's CSV projection profile remains an exchange-format
contract and does not require a WinAMS installation.

Stub mode is deterministic or local-model candidates. `stub_model` and
`stub_max_rounds` control stub proposals; `repair_model` and
`max_repair_rounds` independently control missing input cases. Both are available
on the LLVM settings page. A batch freezes the effective configuration and
versioned requirements when queued.

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

## Verification

Run focused PostgreSQL lifecycle/configuration tests and frontend regressions,
then the repository CI contract. Actual extractor/LLVM integration tests use
repository-owned synthetic C, typed appended inputs and scripted model replies.
Negative tests prohibit original-compiler/OMF/WinAMS calls and check missing
tools, unsupported ABI, evidence drift, policy requirements and model outages.
No old-job migration or database cleanup is part of this change.
