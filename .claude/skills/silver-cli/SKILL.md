---
name: silver-cli
description: Use the Silver Test Platform CLI to search projects and runs, run Silver tests, report steps with results, edit steps, rerun, and drive the unit-test pipeline (configure, generate, evidence, revise, review, export). Trigger on silver_cli, Silver CLI, silver-cli.pyz, running or searching tests, test reports, failed test investigation, jdgrslt, 手顺, 测试结果, 失败测试, 查看日志, 重跑, 单元测试生成, or ut job when the request concerns this platform. Does not cover server deployment or direct database administration.
---

# Silver CLI

Run `python -m silver_cli` from the repository root, or `python silver-cli.pyz`
downloaded from the target server (`GET /api/v1/cli/silver-cli.pyz`), with
Python 3.10+. The CLI uses only the standard library and calls an already
running web service. Read [the CLI guide](../../../docs/cli.md) for the full
command and exit-code contract. Use `--help` for current arguments and
examples. Discover lower-level operations offline with `list`, `list --group
tasks`, and `describe OPERATION`; the server's own `.pyz` always matches its API.

## Connection

- `SILVER_CLI_URL`: explicit target web URL; `--url` overrides it.
- `SILVER_CLI_USERNAME` and `SILVER_CLI_PASSWORD`: inject both through the caller
  or secret manager. Never print, persist, or put real passwords in arguments.
- `login --url URL --username NAME` exchanges the password once (hidden prompt;
  non-interactive callers use `--password-stdin` or `SILVER_CLI_PASSWORD`) for a
  saved personal token. After `login`, commands authenticate automatically; do
  not ask for a password. `logout` revokes and deletes the saved token.
- Prefer `SILVER_CLI_TOKEN` (a personal API token from `token create`) for
  unattended work: no per-command login and no lockout risk. Never print it.
  Precedence: `SILVER_CLI_TOKEN` > username/password > saved login.
- A token cannot manage accounts or credentials: `token create` and admin user
  create/update/delete return exit 4 `PERMISSION_DENIED` under token auth. Ask
  the user to run those with username/password; never work around it. An admin
  password reset revokes that user's tokens, so a later exit 3 means log in again.
- With username/password, each invocation logs in and keeps cookies/CSRF only in memory. The CLI does
  not read `.env`, guess a URL, or connect to PostgreSQL directly.
- Prefer HTTPS. Loopback HTTP is allowed; use `--allow-http` for other trusted
  HTTP targets only when the user has accepted the plaintext transport risk.

## Inspect and repair a failed test

1. Establish the target project with `project list` and `project show PID`.
   Run `task list PID --status failed` and `task failures PID`; inspect failed
   and error results, `jdgrslt`, and error messages. Check truncation metadata
   before claiming all failures were examined.
2. For a selected task key, run `task show PID TID`,
   `task failures PID --task TID`, and `task log PID TID --tail 100`.
   A task key looks like `T000001`. Execution status and judge verdict differ.
3. Find the matching matrix row using `item list PID --sheet test --page 1
   --page-size 100`; continue pages as needed. Match the task's `test_id` to the
   item's case identity, then inspect `item show PID IID` and `steps show PID
   IID`. Item IDs and task keys are different identifiers.
4. Save a candidate steps document in a local UTF-8 JSON file; preserve
   `input_signals`, `expected_signals`, `steps`, and existing project-specific
   fields. Run `steps set PID IID --json @steps.json --dry-run`. For one field,
   use `steps edit PID IID --step N --set KEY=VALUE --dry-run`; repeat `--set`
   for multiple fields. Step positions start at 1. Values parse as JSON when
   valid, otherwise as text; keys are literal fields rather than dotted paths.
   Change the structure with `steps add PID IID --json @step.json [--at N]`,
   `steps remove PID IID --step N` and `steps move PID IID --step N --to M`.
   Every steps command accepts `--test-id TC-1` instead of `IID`; copy the field
   names that existing steps use when writing a new step.
5. Review the proposed changes under the user's authorized scope. Replace
   `--dry-run` with `--confirm`, then read `steps show` again. These flags are
   mutually exclusive. The CLI reads
   the current item version and PATCHes `{version, changes:{steps:document}}`.
6. Preview `task rerun PID TID --dry-run`, then use `--confirm` when authorized.
   Rerun regenerates input from current rows and uses the original model.
   Check `created`, `skipped`, `missing`, and `errors`; query status, logs, and
   judge results after execution. Report observed evidence and remaining
   real-Silver/license validation boundaries.

## Find, run and report Silver tests

Default flow: `project list [--search TEXT]` -> `case list PID [--failed]` ->
`case show PID TEST_ID [--context]` (purpose and other fields, steps, latest
verdict; `--context` adds the project's const, io and lib rows) -> edit with `steps ... --test-id TEST_ID` -> `task run
PID --test-id TEST_ID --wait --confirm`. `PID` may be the project code (for
example `BRK`) or its numeric ID. Use the test id everywhere; item ids and task
keys are only needed for low-level calls.

- Search: `project list --search TEXT`; `task search [--project PID] [--q TEXT]
  [--status failed] [--mine]` searches runs across all visible projects.
- Run: `model list PID` shows saved models; `task run PID --test-id TC-1
  [--test-id ...] [--model NAME@VERSION] --dry-run`, then `--confirm --wait`.
  `task wait PID TID...` waits for existing runs. Both exit 8 when any run did
  not pass and report status, verdict, jdgrslt and errors per run.
- Edit: `case set PID TEST_ID --set purpose=...`, `case add/copy/delete`,
  and `row set PID --sheet const|io|lib --key NAME --set FIELD=VALUE`. All use
  the item version check; never retry a `VERSION_CONFLICT` blindly.
- Batch: `task run PID --failed|--not-run|--all --confirm`. Inspect
  `failed_checks` (step, target, expected, observed) before editing steps.
- Report: `task report PID [--test-id ...] [--status failed|not_run]
  [--output report.md]` lists each case's steps with its latest run, verdict
  and errors. Read results from the platform; do not rerun only to learn them.

## Unit-test pipeline

Execution is Clang/LLVM on the host; the platform does not process WinAMS.

1. `ut options PID` and `ut config PID`; save changes with `ut configure PID
   --json @cfg.json` (fields are listed in `ut configure --help`). Check a
   manifest first with `ut validate-manifest PID --json @manifest.json`;
   browse server paths with `ut paths PID --path DIR`.
2. `ut import-targets PID --confirm`, then `ut targets PID [--function F]`.
3. `ut run PID --function F [--source PATH] --confirm --wait`. A function name
   must match exactly one indexed target; otherwise use `--source` or
   `--target-id`. `ut wait PID --batch B` waits later.
4. Inspect `ut jobs PID --state awaiting_review`, `ut job PID JID`, and
   `ut evidence PID JID` (candidate CSV text, diagnostic, coverage).
5. Fix with `ut revise PID JID --csv Foo.csv --note ... --confirm`; also
   `ut retry`, `ut stop`, `ut stop-batch`.
6. `ut review PID JID --decision approve|reject --note ... --confirm` records a
   human decision under the logged-in account and pins the attempt it read.
   Never approve without a person's explicit consent for that job.
7. `ut export PID [--batch B] --output approved.zip`; `ut stats PID`.

## Write safety

All writes require `--confirm`, including project creation, steps writes, and
reruns. First use `--dry-run`: it makes zero network requests, including no
login and no version read. It validates local input and displays the plan;
it cannot prove remote business validity. Confirmation permits a request; it
does not grant permissions or authorize broader changes. Follow the user's
existing authorization; ask only when the required mutation exceeds it.

Versioned field PATCHes (`steps set` and low-level `items.patch_item`) work
while a collaboration room is active; the server mirrors the accepted cells
into the live editor. The version check sees the database row, which trails
the editor by a few seconds, so a PATCH can replace a cell a collaborator just
changed; `steps` is replaced as one field. Tell the user when you patch a
project that is being edited live.

`COLLAB_ACTIVE` (409) still covers structural and bulk writes: creating,
deleting, moving or duplicating rows, Excel import commit, I/O extraction and
recycle-bin row restore. While a collaboration room is active, stop and retry
only after collaborative editing ends; never send `X-Collab-Reconcile`, which
is reserved for the collaborative editor that reconciles rows into the Y.Doc.
Writes made while no room is active are picked up by the editor the next time
the project is opened.

Never bypass server permissions, project locks, `COLLAB_ACTIVE`, or the human
review/apply boundary for generated AI content. On `VERSION_CONFLICT`, stop,
fetch the latest document, and reconcile; never overwrite by guessing a version
or retrying automatically. After a network failure during a write, inspect
remote state before deciding whether another request is necessary. Do not
automatically repeat reruns, project creation, approval, deletion, or uploads.

## Output and failures

Except for `--help`, stdout is JSON (`success`, `data`, `error`, `request_id`);
streams use JSON Lines. Inspect both exit status and business data. Never infer
test PASS from a successful HTTP request, completed task, or finished stream.
A run whose judge output stops before the overall verdict line (timeout, crash,
killed Silver process) has verdict `UNKNOWN` and is finalised as FAILED, even
when every step it reached passed. Treat it as incomplete evidence: read
`task log` and rerun once the cause is fixed, never report it as passing.

| Exit | Action |
|---|---|
| 0 | Inspect business payload and verdict; successful request is not test acceptance |
| 2 | Fix arguments, JSON/file input, missing URL/credentials, or `CONFIRM_REQUIRED`; HTTP 4xx may also use this code |
| 3 | Fix authentication or invalid login response |
| 4 | Report insufficient permission; do not elevate or bypass |
| 5 | Inspect `error.code`: `VERSION_CONFLICT`, another HTTP 409 lock/conflict, or account lock; reconcile before retrying |
| 6 | Network, redirect, or broken stream; verify whether a write took effect |
| 7 | Server/protocol failure; preserve `request_id` for diagnosis |
| 8 | `wait` reached a failure state, or `task run --wait`/`task wait` saw a run that did not pass |
| 9 | Wait deadline or stream limit; remote work may continue |
| 130 | Client interrupted; remote work may continue |

For exit 2, `INVALID_JSON` and `INVALID_STEPS` require repairing the local
document; `INVALID_PAIR` requires valid, unique `KEY=VALUE` assignments.
`STEP_NOT_FOUND` means the one-based position exceeds the remote document;
`NOT_FOUND` means the item is absent from this project's live rows. For exit 7,
`INVALID_RESPONSE` may mean that the remote item has no valid integer version;
the CLI refuses the write. Preserve the specific code instead of treating all
failures as a transient connection issue.

Keep server error codes and request IDs in reports. An unavailable `jdgrslt`
report is missing evidence, not proof of success. Use low-level `wait` only
after inspecting the server's actual status vocabulary and supplying explicit
success, failure, and pending states; never invent those states.
