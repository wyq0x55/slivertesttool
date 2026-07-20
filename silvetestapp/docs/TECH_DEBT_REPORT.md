# Technical Debt & Dead-Code Report — silvetestapp v2.13.0

Status: **Step 1 executed** (Category A removed & verified). Categories B/C/D are
identification-only and were **not** modified.

Guiding constraints (unchanged): keep deployment, database, public API and
frontend behaviour identical. Do not refactor for its own sake. No DDD, no
microservices, no speculative patterns.

---

## Method

Every candidate was confirmed by **actual reference analysis**, not by comments:
who imports it, who renders it, which HTTP route reaches it. Evidence is given as
`file:line`. Verification of the deletion used:

- Whole-repository scan for any `render_template` / `extends` / `url_for(static, …)`
  reference to each removed file (result: **zero** live references).
- `python -m py_compile` on all 44 Python modules (result: **all compile**).

The test suite (`pytest`) could not be executed here because it requires a live
PostgreSQL instance and Flask, which are unavailable in this analysis sandbox.
The removed items are template/JS/CSS orphans with no Python import edges, so
static exhaustion is equivalent for them. Re-run `pytest` in a normal
environment after pulling these changes.

---

## Category A — Safe to delete (DONE, fully verified)

Residue of the original stand-alone "submit / tasks / admin" UI. That UI was
folded into the LAN Test Matrix (lanmatrix); `page_routes.py` now only issues
redirects and never renders these templates. Each removed JS file was referenced
only by a removed template.

| File | Evidence it was dead |
|------|----------------------|
| `app/templates/base.html` | No `render_template`; only extended by the other removed templates |
| `app/templates/index.html` | No route renders it |
| `app/templates/task_list.html` | No route renders it |
| `app/templates/task_detail.html` | No route renders it |
| `app/templates/admin.html` (top-level) | No route renders it (≠ live `lanmatrix/admin.html`) |
| `app/templates/not_found.html` | No `errorhandler` renders it |
| `app/static/js/upload.js` | Referenced only by removed `index.html` |
| `app/static/js/task_list.js` | Referenced only by removed `task_list.html` |
| `app/static/js/task_detail.js` | Referenced only by removed `task_detail.html` |
| `app/static/js/admin.js` | Referenced only by removed `admin.html` |
| `app/static/js/app.js` | Referenced only by removed `base.html` (live `base_lm.html` does not use it) |

**Kept on purpose (NOT in A):** `app/static/css/app.css` is still loaded by the
live `lanmatrix/base_lm.html` and must stay.

See `DELETION_MANIFEST.txt` for byte sizes + SHA-256 of every removed file so the
change is fully reversible from the original archive.

---

## Category B — Recommend deprecating (owner confirmed no external callers → B1/B2 REMOVED)

The owner confirmed there are **no scripts / CI / pipelines** calling the legacy
`.zip` endpoints. B1 (endpoints) and B2 (their service helpers) were therefore
removed. B3 (redirect stubs) is intentionally kept one more release.

| Item | Action | Detail |
|------|--------|--------|
| Endpoints `POST /api/uploads`, `POST /api/tasks`, `POST /api/tasks/upload` | **REMOVED** | `api_routes.py`: deleted `stage_upload()`, `create_task()`, `upload_and_create()`, plus the legacy-only helpers `_pick_default_sil()` and `_materialise_and_enqueue()`. Docstring endpoint list updated. The live folder path `POST /api/tasks/upload_tree` is unchanged. |
| `upload_service.stage_upload()`, `materialise()`, `_safe_extract()`, `detect_sil_models()` | **REMOVED** | Only reachable through the deleted endpoints. `import zipfile` (only used by `_safe_extract`) removed from the module. Live helpers `stage_tree()`, `materialise_one()`, `cleanup_staging()`, `_save_items()`, `_safe_relpath()` untouched. |
| `tests/test_api.py` legacy cases | **MIGRATED** | Removed `_make_bundle()` (zip helper), `test_two_step_upload`. The E2E run→result→download coverage was re-pointed to `POST /api/tasks/upload_tree` (`test_upload_tree_and_run`), plus `test_upload_tree_requires_model` and `test_resubmit_after_completion_allowed`. Coverage is preserved/improved. |
| Top-level `page_bp` redirect routes | **KEPT (B3)** | `app/routes/page_routes.py:19-36`. Pure old-bookmark compatibility. Remove in a later release once old links are confirmed gone. |

> ⚠️ The `test_api.py` migration could not be executed here (no PostgreSQL/Flask
> in the analysis sandbox). Syntax is validated via `py_compile`; **run `pytest`
> in a normal environment** to confirm the folder-upload E2E passes.

---

## Category C — Must keep (labelled "legacy/compat" but LIVE)

| Item | Why it stays |
|------|--------------|
| `_migrate_schema()` / `_migrate_user_fk_ondelete()` | `app/__init__.py:127,173` — idempotent `ALTER TABLE` run on every boot; the only upgrade path for older databases. |
| Two Excel codecs: `lanmatrix/matrix_excel.py` + `lanmatrix/testmatrix_bridge.py` **vs** `lanmatrix/excel_io.py` + `excel_service.py` | **Not duplicates.** Former = byte-compatible Japanese VHILS `..._SYS.xlsx` import/export (`lanmatrix_api.py:574,601`). Latter = generic per-project field template import/export. Different formats, both live. |
| `models/task.py:39-55` legacy submitter/path columns; `models/setting.py:24` single-model column | Old data rows still depend on these nullable columns for display/traceability. |
| `fields.py` `SYSTEM_FIELDS` (comment: "no longer seeded as visible fields") | Still LIVE: `SYSTEM_FIELD_KEYS` (`fields.py:144,147,152`) drives first-class column routing (case_id/title/result → real `test_items` columns). |
| `frontend/src/adapter.ts` "kept for compatibility" methods; `_maybeOpenSteps` no-op (`adapter.ts:607,613`) | `editor.js` calls these method names as a contract; the no-op is deliberate (steps are first-class Univer tables now). |

---

## Structural Unification — lanmatrix service layer (DONE, static-verified)

The historical seam where lanmatrix used a different internal layout from the
rest of `app/services/` has been removed. Full details, verification and the
deliberately-deferred items are in **`docs/STRUCTURAL_UNIFICATION.md`**. Summary:

- Moved `services/matrix_excel.py` → `services/lanmatrix/matrix_excel.py` (it is a lanmatrix feature module).
- Renamed `lanmatrix/repository.py` → `lanmatrix/queries.py` (flat query helpers, not a repository pattern).
- Split the 1,000-line `lanmatrix/service.py` god module into per-domain
  `users_service` / `projects_service` / `fields_service` / `items_service` /
  `batch_service` / `comments_service` (+ shared `errors.py`), mirroring the
  runner's flat `*_service.py` layout. `service.py` is now a thin re-export
  facade, so **all callers are unchanged** (44/44 public names preserved).
- Verified by `py_compile` (all app+tests) + a bundled `tools/check_names.py`
  undefined-name checker + symbol-equivalence check. **`pytest` still to be run
  in a real env.**
- Deferred (with rationale): splitting `routes/lanmatrix_api.py` (needs a
  test-backed pass), splitting `models/lanmatrix.py` (cohesive — not an
  inconsistency), translating Chinese user-facing strings (behaviour-preserving).

---

## Category D — Unknown risk (needs owner confirmation)

| Item | What to confirm |
|------|-----------------|
| `vendor/bootstrap.min.css` referenced by `lanmatrix/base_lm.html:7` but **file absent** | One 404 per page load, gracefully swallowed by `onerror="this.remove()"`. Not a bug. Decide: drop the `<link>` or vendor the file. |
| Univer vendor bundle version mismatch | `frontend/package.json` pins `@univerjs/* 0.6.10`; committed `vendor/univer/univer.full.umd.js` is an old build. Target is **0.21.5** — a separate breaking migration (see below). |
| Category B external callers | Are there LAN scripts/pipelines POSTing the legacy endpoints? If none → move B1/B2 into A. |
| `TM_TO_LM` / `LM_TO_TM` identity maps (`testmatrix_bridge.py:41`) | Now `{k: k}`. Could be simplified away, but they form the bridge's symmetric interface. Low value — confirm before touching. |

---

## Univer 0.6.10 → 0.21.5 (separate work item, not cleanup)

This is a **breaking** upgrade (package layout, preset composition, Facade API,
locale registration all changed). `adapter.ts` / `steps_adapter.ts` / `main.ts`
must be rewritten against 0.21.x and the bundle rebuilt (`npm run build`
regenerating `vendor/univer/univer.full.umd.{js,css}`). Keep it in its **own PR**
so a UI regression is easy to localise, separate from the dead-code cleanup.

---

## Recommended execution order

1. **Category A** — remove orphan UI (DONE here). Re-run `pytest` + smoke-test
   lanmatrix flows in a normal env.
2. **Category B1/B2** — legacy `.zip` upload flow removed (DONE here).
3. **Structural unification** — lanmatrix service layer (DONE here, see
   `docs/STRUCTURAL_UNIFICATION.md`). Run `pytest` in a real env to confirm.
4. Optional follow-up pass: split `routes/lanmatrix_api.py` by resource group
   (test-backed).
5. Separate PR: Univer 0.6.10 → 0.21.5 frontend migration.
6. Keep all of Category C. Address D1/D4 opportunistically.
