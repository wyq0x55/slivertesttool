# Repository Agent Contract

Validated against `main` at `71dc3cb67ea3` on 2026-09-23.

This file defines repository-specific engineering constraints. Prefer the
current code and tests over historical design notes when they disagree.

## Product and runtime facts

- Validated Python runtime: **CPython 3.10.18**.
- Production persistence is **PostgreSQL only**. Do not add a production SQLite
  fallback for convenience. Small isolated tests may use SQLite when the test is
  explicitly not asserting PostgreSQL-specific behavior.
- The web process is Flask/Waitress. Background execution is Huey with its queue
  stored in PostgreSQL.
- Synopsys Silver execution belongs to `app/runners/`; GitHub CI uses the mock
  runner and does not prove real Silver/license behavior.
- Real-time LAN Matrix collaboration is optional and runs in the separate
  `run_collab.py` ASGI process. It must stay single-worker while Y.Doc rooms
  are process-local.
- The LAN Matrix frontend is built with Vite and Univer **0.25.1**. Node.js is a
  build-time dependency, not a production server dependency.

## Lifecycle ownership

`create_app()` is a side-effect-light application factory. Persistent
initialization is explicit:

- `python run.py` calls `app.bootstrap.bootstrap_app()` once before starting
  worker/collab children.
- Split deployments run `python manage.py bootstrap` once before starting
  `run_web.py`, `run_worker.py`, or `run_collab.py`.
- Web, worker, and collab entry points must not independently create/migrate
  application or Huey schema, seed data, or perform filesystem migrations.

Do not reintroduce startup races by moving persistent bootstrap work back into
`create_app()` or import-time module code.

## Ownership boundaries

- **Routes**: HTTP translation, authentication/authorization, request parsing,
  response/status mapping.
- **Services**: business rules and application workflows.
- **Models**: persistence structure and model-local data behavior.
- **Runners**: Silver process/runtime execution.
- **AI**: generate -> machine validate/retry -> persist draft -> human
  review/approve -> apply through the existing service layer. AI output is never
  authoritative merely because it was generated.
- **CRDT collaboration**: collaborative editing/materialization. Preserve the
  single-writer boundary when a project is actively collaborative.

The `lanmatrix_projects` Flask blueprint name and endpoint identities are a
public route contract. Project resource routes are intentionally split across
`projects.py`, `fields.py`, `models.py`, `items.py`,
`imports_exports.py`, `audit_trash.py`, `members.py`, `reviews.py`, and
`dashboard.py`, with `projects_items.py` acting only as the aggregator.

## Dependency ownership

- `pyproject.toml` is the only hand-maintained direct dependency source.
- `uv.lock` is the frozen resolution.
- `requirements.txt` is generated for pip/offline installation.
- Regenerate with `uv lock` and `python scripts/export_requirements.py`.
- Never hand-edit `requirements.txt`; CI checks lock/export drift.

## Change discipline

- Prefer the smallest complete change that preserves architectural ownership.
- Preserve public HTTP, DB, file, and generated-artifact contracts unless the
  issue explicitly changes them.
- Do not add compatibility/fallback paths without a known caller and an explicit
  reason to keep them.
- Do not hide a structural error behind a fallback, test skip, or duplicated
  implementation.
- Existing Chinese user-visible product copy may remain Chinese; new code,
  comments, identifiers, and technical documentation should be English.
- When requirements are incomplete, make the smallest reasonable engineering
  assumption and keep moving instead of creating speculative branches.

## Verification

- Run targeted tests while developing.
- Before merge, rely on the GitHub CI contract: PostgreSQL 16 + mock runner,
  dependency drift check, entrypoint import smoke, full pytest, and frontend
  production build.
- PostgreSQL-specific migrations/JSONB/query behavior must be validated against
  PostgreSQL, not inferred from SQLite-only tests.
- Real Silver, proprietary DLL/SBS/SIL assets, and license occupancy remain an
  internal/manual validation boundary.

## Delivery

Use normal Git branches, commits, issues, and pull requests. Do **not** create
special delivery-copy directories, incremented duplicate filenames, or
whole-project ZIP archives unless a user explicitly requests an archive artifact.
