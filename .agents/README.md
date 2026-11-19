# Silver Test Platform subagents

Pick a template by the question the child should answer. Each Markdown file carries its `id`, display `name`, and default `model` routing group in YAML front matter. A `model` entry is a default subject to availability and the run's model budget.

| Task | Template | T3 `role` | Default model | Access |
| --- | --- | --- | --- | --- |
| Trace a symptom to its owning module | [Platform investigation](platform-investigation.md) | `research` | `group/impl-deep` | Read-only |
| Modernize a verified frontend pattern | [Frontend modernization](frontend-modernization.md) | `implementation` | `group/impl-routine` | Assigned files |
| Fix a Flask service, PostgreSQL workflow, or bootstrap regression | [Backend workflow](backend-workflow.md) | `implementation` | `group/impl-deep` | Assigned files |
| Fix a lost edit or stale LAN Matrix write | [LAN Matrix consistency](lanmatrix-consistency.md) | `implementation` | `group/impl-deep` | Assigned files |
| Repair a unit-test generation, review, or export step | [Unit-test pipeline](unit-test-pipeline.md) | `implementation` | `group/impl-deep` | Assigned files |
| Reproduce a runner failure and interpret mock evidence | [Silver runner verification](silver-runner-verification.md) | `test` | `group/auto-flash` | Read-only |
| Assess a patch and the evidence for its acceptance | [Acceptance review](acceptance-review.md) | `review` | `group/review-strict` | Read-only |

## Dispatch

Read the chosen template. Check `orchestrator_capabilities` before the first `delegate_task` in the thread. Select the table's `role` and explicitly set `target.providerInstanceId` and `target.model` to a supported, child-capable pair that fits the task and the available quota. Report unavailable requested models. The child receives only `task`, so include the template body, objective, input paths or revision, exact write scope, and acceptance condition.

Match each routing group's exact provider-scoped model ID from `orchestrator_capabilities` (`group/<id>` on `magpie-codex`, `group/<id>[1m]` on `magpie`, and `magpie/group/<id>` on `pi` where exposed). Prefer `providerInstanceId: "magpie"` for `group/impl-routine` and `group/auto-flash` so fallback reaches all configured DeepSeek pools (`raccoon/sn-deepseek-v4-1-flash-1017`, `workbuddy/deepseek-v4.1-flash`, and `dumate/deepseek-v41-flash`). For `acceptance-review`, use `group/review-strict` by default; when implementation used `group/impl-deep` and strict cross-pool independence from `agy-cli` is required, override the reviewer model to `codex/gpt-6.2-sol` or `grok/grok-4.7@500k`. For lightweight read-only tracing or targeted regression runs, `group/impl-routine` or `group/auto-flash` may be substituted to preserve deep-pool quota.

For a read-only review of uncommitted changes, use `isolation: "shared"` and specify the diff to inspect. For parallel writers, assign disjoint files, including tests. A separate `worktree` starts at the parent commit. Supply any required uncommitted input explicitly. The root agent owns cross-component decisions, final diff inspection, and delivery.

For rolling modernization, the root agent adjusts active concurrency as findings are verified and work completes. Dispatch more independent items while test resources and review capacity are available. Serialize items that share files or depend on each other, and slow dispatch when verification or review backs up. Apply the host's limits and count all delegated roles toward any cumulative budget set for the run.

Track the delegate count and model usage separately. Before starting a batch, record the provider and model for implementation and verification, account for pools that share quota, and reserve capacity for required independent verification. Record the model actually used when the host reports it. When no suitable model fits the remaining quota, keep the affected candidates queued, report the constraint, and continue with applicable scans and deterministic checks.

Example request to the root agent:

> Use `.agents/lanmatrix-consistency.md` to investigate a stale cell after a room reopens. Limit edits to `app/collab/doc_model.py` and `tests/test_collab_hydration_reconcile.py`. Show which value wins on either side of the sync mark, and run the focused regression.
