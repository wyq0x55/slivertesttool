# Structural Unification — LAN Test Matrix service layer

Status: **applied** to the cleaned tree. Behaviour, HTTP API, database schema,
deployment and frontend are unchanged. This pass only reorganises Python
modules inside `app/services/` so the LAN Test Matrix (lanmatrix) feature
follows the same conventions as the rest of the platform.

## Why

`app/services/` mixed two conventions:

* **Silver test runner** — flat, single-responsibility function modules
  (`task_service.py`, `upload_service.py`, `model_service.py`, …).
* **LAN Test Matrix** — a `lanmatrix/` sub-package whose logic was concentrated
  in one 1,000-line `service.py` "god module", plus a `repository.py` whose
  name implied a repository/DDD layer it does not actually use, plus a feature
  module (`matrix_excel.py`) that had leaked up to the flat top level.

This was the historical seam left over from lanmatrix once being a self-contained
standalone app. The three items below remove that seam without changing any
external contract.

## Changes applied

| # | Change | Rationale | Import sites updated |
|---|--------|-----------|----------------------|
| 1 | Moved `app/services/matrix_excel.py` → `app/services/lanmatrix/matrix_excel.py` | It is a lanmatrix feature module (byte-compatible VHILS Excel codec), used only by `lanmatrix/testmatrix_bridge.py`. It belongs inside the feature package. | `testmatrix_bridge.py` (`from .. import` → `from . import`, 2 sites); `tests/test_matrix_excel.py` module path; doc/comment refs |
| 2 | Renamed `lanmatrix/repository.py` → `lanmatrix/queries.py` | The content is flat query helpers (`parse_sort`, `apply_sort`, `build_filter_clause`), not a repository pattern. The old name implied a layer that isn't there and clashed with the flat-function style used everywhere else. | `items_service.py` (`repository.` → `queries.`, 2 sites) |
| 3 | Split `lanmatrix/service.py` (1,000 LOC) into per-domain `*_service.py` modules mirroring the runner layout, keeping `service.py` as a thin compatibility facade | Removes the god module; each domain is now a small, cohesive file that reads like the runner services. | none — see below |

### The service split

`service.py` was already cleanly sectioned; each domain was a contiguous block,
so the code was **sliced verbatim** (no logic was rewritten) into:

| New module | Responsibility | Source lines |
|------------|----------------|--------------|
| `errors.py` | `ServiceError`, `VersionConflict` (shared exception types) | 30–45 |
| `users_service.py` | users, membership roles, system-admin account CRUD | 48–339 |
| `projects_service.py` | project lifecycle + default-field seeding | 342–434 |
| `fields_service.py` | per-project field-definition CRUD | 437–528 |
| `items_service.py` | row query, single-row CRUD (validation + optimistic lock + audit), multi-row ops | 531–828 |
| `batch_service.py` | batch search/replace preview / apply / undo | 831–963 |
| `comments_service.py` | cell comments + audit-log query | 966–1000 |

`service.py` now only re-exports these:

```python
from .errors import ServiceError, VersionConflict
from .users_service import *
from .projects_service import *
from .fields_service import *
from .items_service import *
from .batch_service import *
from .comments_service import *
```

So every existing caller — `routes/lanmatrix_api.py`, `routes/lanmatrix_pages.py`,
`testmatrix_bridge.py`, `excel_service.py` — that references `service.<name>`,
`service.ServiceError` or imports `from ...service import ServiceError,
VersionConflict` keeps working with **zero changes**.

Per-module `_utcnow()` helpers were added where needed, matching the runner's
existing convention (each `*_service.py` defines its own).

## Verification (static — no test DB in this environment)

* `py_compile` passes for **all** modules in `app/` and `tests/`.
* A custom `symtable`-based checker (bundled as `tools/check_names.py`) confirms
  **no undefined global names** in any lanmatrix module — i.e. no import was
  missed during the slice. The checker was validated by deliberately dropping an
  import and confirming it flags the resulting names.
* Symbol-equivalence check: the facade exposes **all 44** public names the
  original `service.py` exported (0 missing).
* Zero residual references to the old `repository` module or the old flat
  `matrix_excel` path anywhere in `app/` or `tests/`.

> A real `pytest` run still requires PostgreSQL + Flask (unavailable in the
> refactoring sandbox). Please run the suite once in a normal environment;
> `tests/test_api.py` and `tests/test_matrix_excel.py` cover the touched paths.

## Deliberately NOT changed (with rationale)

These are lanmatrix-named but are **not** layering inconsistencies, so touching
them would be change-for-its-own-sake or unacceptable risk:

* **`app/models/lanmatrix.py`** (8 tightly-related models: `LMUser`,
  `ProjectMember`, `Project`, `FieldDefinition`, `TestItemRow`, `CellComment`,
  `AuditLog`, `DataJob`). This is a cohesive, feature-grouped model file — the
  same shape as the runner's `task.py`/`task_event.py`/`setting.py`. Splitting
  eight FK-linked models into separate files would reduce locality for no gain.
  **Kept.**
* **`app/routes/lanmatrix_api.py` (1,264 LOC) / `lanmatrix_pages.py`.** These are
  feature route files, structurally consistent with `api_routes.py` /
  `page_routes.py`. The size of `lanmatrix_api.py` is a god-file smell, but
  splitting a single Flask blueprint across files can silently drop endpoints
  (a missed side-effect import → 404 that only appears at runtime) and cannot be
  validated here. **Recommended as a separate, test-backed pass** (split by
  resource group — auth / projects / fields / items / batch / import-export —
  keeping the one `v1` blueprint and identical URLs).
* **Chinese user-facing strings** in the service/bridge modules (e.g.
  `"该记录已被其他用户修改"`). These are the messages shown to the Chinese-speaking
  LAN users; translating them would change the product's behaviour/UX. Left
  as-is intentionally. (Project policy asks for English in *new* code; existing
  user-visible copy is preserved.)
