# Silver Test Platform subagents

Pick a template by the question the child should answer. Each Markdown file carries its `id`, display `name`, and default `model` in YAML front matter.

| Task | Template | T3 `role` | Access |
| --- | --- | --- | --- |
| Trace a symptom to its owning module | [Platform investigation](platform-investigation.md) | `research` | Read-only |
| Fix a Flask service, PostgreSQL workflow, or bootstrap regression | [Backend workflow](backend-workflow.md) | `implementation` | Assigned files |
| Fix a lost edit or stale LAN Matrix write | [LAN Matrix consistency](lanmatrix-consistency.md) | `implementation` | Assigned files |
| Repair a unit-test generation, review, or export step | [Unit-test pipeline](unit-test-pipeline.md) | `implementation` | Assigned files |
| Reproduce a runner failure and interpret mock evidence | [Silver runner verification](silver-runner-verification.md) | `test` | Read-only |
| Assess a patch and the evidence for its acceptance | [Acceptance review](acceptance-review.md) | `review` | Read-only |

## Dispatch

Read the chosen template. Check `orchestrator_capabilities` before the first `delegate_task` in the thread. Select the table's `role` and pass the front matter's `model` as `target.model` on a child-capable provider. Report an unavailable model to the user. The child receives only `task`, so include the template body, objective, input paths or revision, exact write scope, and acceptance condition.

For a read-only review of uncommitted changes, use `isolation: "shared"` and specify the diff to inspect. For parallel writers, assign disjoint files, including tests. A separate `worktree` starts at the parent commit. Supply any required uncommitted input explicitly. The root agent owns cross-component decisions, final diff inspection, and delivery.

Example request to the root agent:

> Use `.agents/lanmatrix-consistency.md` to investigate a stale cell after a room reopens. Limit edits to `app/collab/doc_model.py` and `tests/test_collab_hydration_reconcile.py`. Show which value wins on either side of the sync mark, and run the focused regression.
