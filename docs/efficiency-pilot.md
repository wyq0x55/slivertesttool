# Owner-selected efficiency pilot

This is a reporting and readiness tool, not an approval or execution command.
It never starts a worker, calls an AI provider, launches Silver, updates rows or
creates a schema. Use an existing database and retained server-owned evidence.
The authoritative input is `app.services.pilot_contract.PilotInput`; obtain its
derived JSON Schema with `python -m scripts.efficiency_pilot --schema`.

## Prepare the owner input

From the repository root, with CPython 3.10.18 and existing dependencies:

```powershell
New-Item -ItemType Directory -Force .cache/pilot | Out-Null
python -m scripts.efficiency_pilot --template | Set-Content -Encoding utf8 .cache/pilot/input.json
```

The template intentionally has null IDs, revisions, approvals and timing. It
cannot pass as a real selection. The suggested ten slots per module are only a
layout: the twenty selected viewpoints may be distributed differently, provided
both distinct modules are represented. Supply the actual project, saved model
ID/version and original SIL SHA-256, plus twenty distinct row IDs/base versions,
module names, document revisions and explicit owner approval references.
Those document/approval references are operator declarations, not URLs to fetch
or instructions to execute; verdict `review_status` is not viewpoint approval.

Record these values rather than deriving them from task/draft timestamps:

- `manual_baseline_minutes`: positive measured manual-workflow wall time.
- `assisted_total_minutes`: positive measured assisted-workflow wall time.
- `human_intervention_minutes`: nonnegative measured person-time effort.
- `human_intervention_count`: nonnegative integer count; measured zero is valid.

Unknown measurements remain null. Person-time can exceed elapsed wall time when
several people participate; the tool does not invent a one-person business rule.
Do not commit the completed manifest, database credentials or proprietary assets.

## Audit explicitly, never by database fallback

Offline validation/reporting is the default:

```powershell
python -m scripts.efficiency_pilot .cache/pilot/input.json
```

Even with complete operator timings, an offline report is `unverified`; database
counts and time-savings claims remain null. Neither `DATABASE_URL`, Huey settings
nor a configured provider can make this command connect or execute anything.

For a live audit, explicitly set the private `PILOT_DATABASE_URL` environment
variable to the intended existing PostgreSQL target. Prefer read-only database
credentials. Both the hostname and database name must be explicit in the URL;
bare/default targets and query routing overrides (`host`, `hostaddr`, `dbname`,
`service`, `servicefile`) are rejected before application creation. Set `$actorId`
to an existing active user's ID with `project.view`
permission on that project, then use:

```powershell
python -m scripts.efficiency_pilot .cache/pilot/input.json --live --actor-id $actorId --mode preflight
```

The local actor selector is a policy check, not a network login or a substitute
for protecting database credentials. The CLI uses a fresh PostgreSQL
repeatable-read, read-only transaction. It performs no bootstrap/migration.
Preflight readiness checks the current selection, not historical verdict review
or permission to launch a test. A ready preflight does not approve rollout.

After explicitly authorized human generation, approval and execution, add the
actual `draft_ids` and `run_attempts` (`task_key`, `run_count`, `item_id`) to the
same baseline input, and run:

```powershell
python -m scripts.efficiency_pilot .cache/pilot/input.json --live --actor-id $actorId
```

Only those listed drafts/attempts are inspected. The collector validates the
existing database-bound manifest/outcome digests, original saved-model identity,
retained result files and causal links to approved generated execution documents.
The authenticated input pinning timestamp must be timezone-aware and follow
the draft's creation and approval/application times. An older identical archive
or unknown ordering cannot certify execution of a later approved draft. Genuine
historical runtime counts remain visible without filling assisted coverage.
Retests count as separate attempts but never as extra completed viewpoints.
Current row versions may advance after applying/running; immutable historical
evidence and current preflight readiness are distinct.

## Interpret the report

Acceptance and conversion rates are fractions from 0 to 1 over distinct
`(draft_id, item_id)` candidates, not a claim that a whole draft was accepted.
Rejected generated candidates remain in the acceptance denominator. Empty
denominators yield null, not zero or perfect success. Conversion validation
reuses the actual production exporter/parser, without executing the runner.

Provider provenance is counters-only metadata captured at generation time.
Missing historical receipts, replaced generators, pure stubs and mixed API/stub
returns cannot be promoted using a current configured model label. An API-path
receipt is not proof of a vendor's internal model behavior. Synthetic runs,
unclassified archives, integrity failures and unrelated historical manual runs
do not fill AI-assisted real-execution coverage.

Complete measurement requires the supplied human timings and authenticated,
approved/converted API-origin procedures causally linked to real retained
attempts covering all twenty selected viewpoints. Genuine failing test verdicts
remain failures; they are not relabelled PASS. Reported time savings can be
negative and remain based on operator-recorded timing, not independently timed
by this tool. `requires_owner_review` is always true and `rollout_approved` is
always false, even when a measurement is complete.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Schema/template emitted, or the requested readiness/measurement check is complete; never rollout approval |
| 2 | Invalid input/CLI usage, unavailable explicit audit target or denied actor access; diagnostics omit raw input and credentials |
| 3 | Valid report with missing readiness or measured evidence; JSON lists missing fields/coverage |

Keep JSON reports with the private manifest and original attempts. Integration
tests use only separate disposable databases and synthetic fixtures; they are
not proof that the owner-selected real efficiency pilot has been executed.
