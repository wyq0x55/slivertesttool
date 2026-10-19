# Unit-test pipeline (ut-agent + WinAMS + local LLM)

The 単体テスト page (`/lanmatrix/projects/<id>/ut`) turns a project's
test index (e.g. `HOYU/N-O2606-PSD-049.csv`) into a 実施一覧 and drives the
selected functions through:

```
実施一覧 ─select→ batch ─ut-agent generate→ candidate TestCsv
   ─ut-agent check (static)→ ─ut-agent execute (WinAMS sandbox)→ ─ut-agent check→
   PASS ─────────────────────────────────────────────→ awaiting_review
   FAIL ─local LLM revision (≤ max_repair_rounds)→ execute/check again
   FAIL after the limit / on a human revision ───────→ needs_human
   WinAMS environment incomplete ────────────────────→ blocked
   bridge / model unreachable ───────────────────────→ error
human gates → upload revision · approve · reject · retry (blocked/error)
```

No machine outcome is final: every path ends at a human gate, and approving a
candidate whose machine verdict is not `PASS` requires a review note.

## Ownership

| Concern | Owner |
| --- | --- |
| Index parsing, generation, format/baseline checks, WinAMS execution, coverage targets | ut-agent, via the `ut-agent bridge` JSON contract (`ut-agent.bridge/1`) |
| 実施一覧, batches, job state, provenance, LLM calls, human review | this platform (`app/services/ut_pipeline/`) |

ut-agent runs in its own interpreter and checkout and is never imported. The
LLM only revises CSVs; its output is a candidate that ut-agent executes and
checks again. The repair prompt is rendered from the constraint pack ut-agent
exports for the bound baseline, which also contains the revision rules its
checker enforces.

## Configuration

Environment (`.env`):

| Key | Meaning |
| --- | --- |
| `UT_AGENT_ROOT` | ut-agent checkout (its `src/` is put on `PYTHONPATH`) |
| `UT_AGENT_PYTHON` | interpreter with ut-agent's dependencies |
| `UT_AGENT_EXTRACTOR` | `ut-clang-extract.exe` used for generation |
| `WINAMS_EXECUTABLE` | `SSTManager.exe` (default `C:\WinAMS\BIN\SSTManager.exe`) |
| `UT_GENERATE_TIMEOUT` / `UT_EXECUTE_TIMEOUT` / `UT_BRIDGE_TIMEOUT` | seconds |
| `UT_REPAIR_MAX_ROUNDS` | default LLM rounds for new project bindings (5) |

The model endpoint is the existing AI setting (OpenAI-compatible
`api_base` / `api_key` / `model`). A project binding may override the model
name (e.g. `qwen3.8-27b-gsq`) for repair only.

Per project (page → ut-agent 项目绑定): ProjectManifest path, index CSV,
Product/Soft root, WinAMS work root (the directory containing the index's
`WinAMS/src/...` unit tree), repair limit and model. A batch freezes the
binding it was queued with.

## Evidence kept per job

Each candidate is an immutable `ut_attempts` row (`GENERATOR`, `LLM`, `HUMAN`)
with its CSV (under `WORKSPACE_DIR/ut/p<project>/b<batch>/j<job>/`), execution
evidence, check verdict and, for LLM rounds, model, token usage, prompt digest,
rationale and declared changes. The batch stores generator provenance
(commit, extractor SHA256, baseline) and the constraint pack.

## Runtime notes

- WinAMS runs and project-wide generation are serialised inside the worker
  process; checks and LLM calls run concurrently on Huey threads.
- A static check runs before WinAMS so a malformed revision does not consume a
  WinAMS run.
- C source is not sent to the model; the prompt carries the CSV, the verdict
  and the execution summary.
