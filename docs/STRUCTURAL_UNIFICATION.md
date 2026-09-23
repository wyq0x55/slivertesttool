# Structural Unification — LAN Test Matrix Service Layer

> **Historical design snapshot — completed.**
>
> Originally written during the LAN Matrix service-layer cleanup. Revalidated
> against `main` at `71dc3cb67ea3` on **2026-09-23**.
> This document records why the service split exists; it is not a current
> backlog. Later follow-up work, especially the route split, has already landed.

## Why this change existed

The repository historically mixed two service conventions:

- runner services were small, flat, single-responsibility modules;
- LAN Matrix business logic had accumulated in one large
  `app/services/lanmatrix/service.py` module plus a misleading
  `repository.py` query-helper name and a misplaced matrix Excel module.

The cleanup removed that seam without changing the public HTTP/DB/file
contracts.

## Applied service-layer changes

| Change | Current result |
|---|---|
| Move matrix Excel logic into `app/services/lanmatrix/matrix_excel.py` | The codec now lives with the LAN Matrix feature that owns it. |
| Rename `repository.py` to `queries.py` | The module is correctly named for its flat query/sort/filter helpers. |
| Split the old ~1,000-line `service.py` | Business logic lives in domain modules; `service.py` is now only a compatibility facade. |

The domain modules remain:

- `users_service.py`
- `projects_service.py`
- `fields_service.py`
- `items_service.py`
- `batch_service.py`
- `comments_service.py`
- `errors.py`

`service.py` intentionally re-exports these APIs for existing callers. That
facade is a compatibility surface, not evidence that the old god module still
exists.

## Subsequent follow-up status

The original document deferred several items because they could not be safely
validated at the time. Their current status is now known:

### Route split — completed

The old recommendation to split a large LAN Matrix API route module is no longer
open work. PR #10 split project-resource ownership while preserving one stable
`lanmatrix_projects` blueprint and endpoint identities.

The current resource modules are:

- `projects.py`
- `fields.py`
- `models.py`
- `items.py`
- `imports_exports.py`
- `audit_trash.py`
- `members.py`
- `reviews.py`
- `dashboard.py`

`projects_items.py` is now only the blueprint aggregator. A route-contract
test freezes the **73** routes belonging to this blueprint by URL, HTTP method,
Flask endpoint identity, and wrapped view identity.

### Model split — still intentionally deferred

`app/models/lanmatrix.py` is large but cohesive. Its related project/test
matrix models share strong foreign-key and workflow locality. Do not split it
solely to reduce line count.

### User-visible Chinese strings — intentionally preserved

Existing Chinese product messages are behavior/UX, not architecture debt.
Repository engineering docs and new code comments should be English, but
translating product copy is a separate product decision.

## Verification now available

The original cleanup was initially static-verified because its sandbox lacked a
live PostgreSQL test environment. That limitation is historical.

The repository now has GitHub CI with:

- PostgreSQL 16
- mock runner
- dependency lock/export verification
- web/worker/collab import smoke
- full pytest
- frontend production build

After the route and dependency follow-ups, the #7 PR gate completed with
**924 passed / 4 skipped**, and the frontend production build was green.

Real Synopsys Silver execution remains outside GitHub CI and must still be
validated in the internal runtime when Silver-specific behavior changes.

## How to use this document

Use it to understand the rationale behind the current service layout. Do not use
old source-line numbers, old module names, or the original deferred route split
as instructions for new work. For current engineering constraints, read
`AGENT.md` and `README.md`, then verify against the code and tests.
