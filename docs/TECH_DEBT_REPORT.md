# Current Technical Debt and Cleanup Status

> **Current-state document, not an historical backlog.**
>
> Validated against `main` at `71dc3cb67ea3` on **2026-09-23**.
> The earlier v2.13 cleanup report contained useful historical evidence but also
> accumulated stale route, bootstrap, test-environment, and Univer assumptions.
> Git history remains the source for that older snapshot.

The governing rule is still: preserve deployment, database, public API, and
frontend behavior unless a dedicated issue explicitly changes a contract. Avoid
refactors whose only benefit is aesthetic layering.

## Current architecture facts

- Production application data and the Huey queue are PostgreSQL-backed.
- `create_app()` is side-effect-light. Persistent schema/data/filesystem/Huey
  initialization is owned by `app.bootstrap.bootstrap_app()` and is run once by
  `run.py` or explicitly through `python manage.py bootstrap`.
- The former LAN Matrix project/items route god-file has been split. The stable
  `lanmatrix_projects` blueprint is aggregated in
  `app/routes/lanmatrix/projects_items.py`; resource handlers live in the nine
  resource modules documented in `AGENT.md`.
- AI generation follows generate -> validate/retry -> draft -> human review ->
  apply. Applying approved output goes through the existing service layer.
- Real-time collaboration is a separate optional ASGI process and uses the same
  PostgreSQL-backed application state.
- Frontend Univer packages and generated bundle logic are currently pinned to
  **0.25.1**. The old “0.6.10 -> 0.21.5” migration plan is obsolete.
- Dependency ownership is `pyproject.toml` -> `uv.lock` -> generated
  `requirements.txt`. CI rejects lock/export drift.

## Resolved historical debt

| Area | Current status |
|---|---|
| Orphan standalone submit/tasks/admin UI | Removed. Live navigation is LAN Matrix based; top-level page routes are redirects only. |
| Legacy staged ZIP upload API | Removed. The primary runner submission path is `POST /api/tasks/upload_tree`. |
| LAN Matrix service god module | Split into domain services; `service.py` remains a compatibility facade for live callers. |
| Field-key identity drift | Resolved: aliases such as `test_name -> title` and `remark -> comment` route to first-class storage/search behavior. |
| Persistent startup ownership | Resolved by #4 / PR #9. Factory construction no longer owns schema/data/filesystem migration. |
| Missing repository CI | Resolved by #6 / PR #11, with the baseline fixes in PR #12. |
| Project/items route god module | Resolved by #5 / PR #10 with a frozen 73-route `lanmatrix_projects` contract. |
| Dependency metadata duplication | Resolved by #7 / PR #13. The migration also exposed and fixed the missing `libclang` lock entry. |
| Old Univer migration item | Obsolete. Current frontend is consistently on Univer 0.25.1 and is production-built in CI. |

## Live compatibility surfaces that are intentionally kept

These are not current removal tasks.

### Top-level page redirects

`app/routes/page_routes.py` keeps `/`, `/tasks`, `/tasks/<key>`, and
`/admin` as redirects for old bookmarks. Remove them only when there is a
known migration plan for callers/bookmarks; do not invent a second UI path.

### LAN Matrix service facade

`app/services/lanmatrix/service.py` re-exports domain service functions for
existing callers. It is small and has a real compatibility role. Removing it is
low value unless callers are intentionally migrated in a dedicated change.

### Cohesive LAN Matrix model module

`app/models/lanmatrix.py` is large, but the models are tightly related through
project/test-matrix foreign keys and workflows. File size alone is not a reason
to split it.

### Bridge identity maps

Identity maps such as `TM_TO_LM` / `LM_TO_TM` may look redundant when they
are 1:1, but they document a symmetric format boundary. Simplify only if doing so
removes real complexity without weakening the bridge contract.

## Remaining validation boundary

GitHub CI validates the reproducible platform surface:

- PostgreSQL 16
- CPython 3.10.18
- mock runner
- frozen dependency/export drift
- web/worker/collab import smoke
- full pytest
- frontend production build

The final #7 PR gate completed with **924 passed / 4 skipped** plus a green
frontend build.

CI intentionally does not launch Synopsys Silver, consume licenses, or execute
proprietary DLL/SBS/SIL assets. Changes to real Silver process control, model
opening, license behavior, or proprietary artifacts still require an internal
runtime check.

## Maintenance rule for this document

Add an item here only when it is current, evidenced, and actionable. Once
resolved, move it to the resolved table with the validating PR/commit rather
than leaving obsolete “future work” instructions in place.
