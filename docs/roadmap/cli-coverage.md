# CLI coverage for AI operators

## Objective and boundaries

Expose every existing registered business HTTP operation through a discoverable,
non-interactive CLI without bypassing authentication, project permissions, CSRF,
optimistic versions, approval, model snapshots, or single-worker scheduling.
The web API remains the business source of truth. Browser layout and collaborative
cursor/CRDT sessions are not business commands; local bootstrap and the offline
efficiency pilot retain their existing entry points.

## Execution plan

1. Inventory registered API operations and derive an offline command catalogue.
2. Add authenticated transport, multipart uploads, artifact downloads and SSE.
3. Add JSON-first command dispatch, dry-run, write confirmation and bounded waits.
4. Verify route parity, subprocess workflows, authorization and transport failures.
5. Publish command examples, coverage boundaries and actual verification evidence.

## Acceptance criteria

- `python -m silver_cli list` and `describe` require neither Flask nor a database.
- Each registered `/api/v1` method/path has exactly one discoverable named operation.
- `call` preserves server response envelopes and uses the actual server identity.
- All writes require `--confirm`; `--dry-run` makes zero HTTP requests, including login.
- Automatic login credentials come from the environment; explicit auth endpoint
  bodies can use JSON file/stdin. Credentials are never persisted or printed.
  No redirect, ambient proxy, silent mutation retry, or credential transmission
  over remote HTTP without explicit opt-in.
- JSON file/stdin, repeated query/form fields, repeated file uploads, non-overwriting
  binary downloads, SSE JSON lines and bounded read-only polling are supported.
- Invalid input, auth, permissions, conflicts, network failures and unsuccessful
  terminal jobs have stable nonzero exit codes.
- Tests use loopback fixtures and a dedicated disposable PostgreSQL database only;
  production data, real Silver and paid AI are outside this implementation run.

## Delivery state

All five steps are complete: 138 registered operations, authenticated streaming
transport, JSON command dispatch and bounded asynchronous observation. Validation
has 85 passing focused tests and 93% combined line/branch coverage; full backend
regression is **1741 passed / 4 skipped / 4 existing warnings**. Operator documentation is available
at [CLI guide](../cli.md); actual evidence is recorded in
[verification](../verification/cli-coverage-2026-10-03.md).

Work remains isolated in a detached worktree based on main
`52d08823b123c8ee71b0cff837cc855734170dbc`. No commit or push is part of this request.
Independent review was unavailable due to provider quota; local security review
and negative regression tests are recorded, not presented as independent sign-off.
