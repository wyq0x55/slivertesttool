# Run write-back, model versioning, and the collaborative-safe write path

This document covers the changes delivered in phases P0-P2. It explains the one
non-obvious design decision (why a finished run cannot simply `UPDATE` a row)
and lists the new configuration, schema and endpoints.

---

## 1. The problem: a direct write is not durable in collaborative mode

`app/collab/materializer.py` reconciles a project's `Y.Doc` into the database on
a 3-second debounce, **in one direction only**. While anybody has the project
open in the collaborative editor, the `Y.Doc` is the authoritative copy of every
row. A worker that writes `TestItemRow.result` directly therefore has its value
overwritten by the next flush, which replays whatever the editor still shows.

This was already latent with the single `result` column. Writing five evidence
columns per run would have made it constant and visible.

### The constraint

The write originates in the **Huey worker** process. The `Y.Doc` only exists in
the **collab (ASGI)** process. They share nothing but the database.

### The solution

```
worker                               database                     collab server
------                               --------                     -------------
record_run()
  |-- apply_server_fields() ------->  lm_test_items   (authoritative)
  |                                   lm_row_writebacks (queued, only
  |                                     when presence says a room is live)
  |                                          |
  |                                          |  <---- claim_pending()  every 2s
  |                                          |                            |
  |                                          |                   write_row_fields()
  |                                          |                   inside mat.suppressed()
  |                                          |                   + doc.transaction()
```

Key properties:

* **The database is always written first and stays authoritative.** The queue
  only mirrors the same values into the Doc so the editor cannot revert them.
* **Nothing is lost if the collab server is down.** The value is in the database
  and a room bootstrapped later reads it from there.
* **Nothing is queued for non-collaborative projects.** `is_collab_active()`
  gates it, so the classic path is unchanged and costs nothing.
* **Stale items are dropped, not replayed.** A write-back older than
  `COLLAB_WRITEBACK_TTL_SECONDS` belongs to a room that never came back;
  replaying it onto a much newer Doc could undo a manual edit.
* The write goes inside `Materializer.suppressed()` — the extension point the
  materializer's own docstring reserves for exactly this case — so it does not
  bounce back through a reconcile.

The drain interval (`COLLAB_WRITEBACK_POLL_SECONDS`, default 2.0s) is
deliberately below the materializer debounce (3s), so a queued value reaches the
Doc before the next reconcile could overwrite it from stale Doc state.

---

## 2. What a finished run now writes

Previously only `result` flowed back, so every other evidence column had to be
typed in by hand. Now one run stamps the full set:

| field key       | Excel column | source                                       |
|-----------------|--------------|----------------------------------------------|
| `result`        | 結果          | the task verdict                             |
| `version_label` | バージョン     | `ProjectModel.version` (omitted if unknown)  |
| `executor`      | 実施者        | submitter display name → username → legacy   |
| `exec_date`     | 実施日        | `finished_at` rendered in `LM_DISPLAY_TZ`    |
| `log`           | ログ          | the originating `task_key`                   |

All five field keys already existed in `matrix_excel.SUMMARY_COLUMNS`; no Excel
layout change was needed.

Two deliberate behaviours:

* **An unknown model version is omitted, not blanked.** Writing `""` would erase
  a label a human had filled in by hand.
* **`exec_date` uses local calendar days.** A run finishing at 23:30 Beijing
  time is reported on that day, not the next one. See §4 on the timezone.

### Row matching performance

The old implementation loaded *every* row of the project and compared in Python
— a full table read on every finished run. It now lets the database narrow to
candidates on `case_id` / `custom_values->>'test_id'` first, then applies the
precedence rule (`test_id` field if set, else `case_id`) to the survivors.

---

## 3. `lm_test_run_records`: why the row columns are not enough

A row's `result` / `executor` / `exec_date` can only ever show the **latest**
run — that is all a spreadsheet cell can express. Every question the dashboard
asks needs the runs before it:

* "how did v1.2 compare with v1.1?"
* "how fast are we burning through the plan?"
* "when did this case last pass?"

So each finished run also appends one immutable record. This table is the single
source of truth for the dashboard; any daily-metrics cache added later is
derived from it and can be rebuilt at any time.

Notable columns:

* `row_uuid` **and** `test_id` are both kept: the former survives a row being
  renamed, the latter survives a row being deleted and re-created.
* `model_name` / `model_version` / `executor_name` are **denormalised copies**,
  not foreign keys, so renaming or deleting a model can never rewrite history.
* `outcome` is a normalised bucket (`pass|fail|error|untestable|cancelled`) so
  verdict parsing lives in one place instead of in every chart.
* `executed_on` is the local calendar date, so burn-up charts bucket by the date
  the user sees rather than by UTC.

`Untestable` is reserved for a case a human has judged impossible to test. No
runner produces it; it is recognised here so the review flow and the dashboard
share one vocabulary.

---

## 4. Model versioning

`lm_project_models` gains `version`, `version_note` and `deprecated_at`.

The version label is **not cosmetic**: it is stamped onto every row the model
produces a verdict for and it is the grouping key of the dashboard's per-version
comparisons. Free text would quietly split one release into several (`v1.0` vs
`v1.0 ` vs `V1.0`), so it is validated against `LM_MODEL_VERSION_PATTERN`.
An empty value is allowed and means "unversioned" — pre-existing models must
keep working.

Two operations beyond creation:

* **Relabelling is audited** (`model.version.update`). Changing a label changes
  how future evidence is grouped, so it is a deliberate, logged operation rather
  than a silent field edit. Historical rows and run records keep the value they
  were stamped with.
* **Retiring uses `deprecated_at`, not `DELETE`.** Deleting a superseded model
  would orphan every run record and row that cites it. A deprecated model
  disappears from the pickers but stays fully resolvable, and is cleared as
  `is_current` so nobody keeps running against it.

---

## 5. New configuration

| key | default | purpose |
|---|---|---|
| `LM_DISPLAY_TZ` | `Asia/Shanghai` | timezone for `実施日` and dashboard day buckets |
| `LM_MODEL_VERSION_PATTERN` | `^[A-Za-z0-9._\-+]{1,64}$` | accepted version labels |
| `LM_WRITEBACK_LOG_COLUMN` | `1` | mirror `task_key` into the `ログ` column |
| `COLLAB_WRITEBACK_POLL_SECONDS` | `2.0` | Doc drain interval (keep < 3s debounce) |
| `COLLAB_WRITEBACK_TTL_SECONDS` | `900` | queued write-backs older than this are dropped |

> **Timezone note.** The Excel column headers are Japanese. If the workbook is
> delivered to a Japanese counterpart, consider `Asia/Tokyo`: it differs from
> `Asia/Shanghai` by one hour, so a run finished between 23:00 and 24:00 Beijing
> time is filed under a different calendar day in each.

Everything is stored in UTC; `LM_DISPLAY_TZ` only decides which calendar day a
run is reported under. An unknown timezone name is logged and degraded to UTC
rather than raised — a misconfiguration must not take a run down with it.

---

## 6. New / changed endpoints

| method | path | capability | purpose |
|---|---|---|---|
| `POST` | `/api/v1/projects/<id>/models` | `model.manage` | now accepts `version`, `version_note` |
| `POST` | `/api/v1/projects/<id>/models/upload` | `model.manage` | now accepts `version`, `version_note` form fields |
| `PATCH` | `/api/v1/projects/<id>/models/version` | `model.manage` | relabel a model (audited) |
| `POST` | `/api/v1/projects/<id>/models/deprecate` | `model.manage` | hide / restore a superseded model |

`ProjectModel.to_dict()` now returns `version`, `version_note`, `deprecated`
and `deprecated_at`.

---

## 7. Schema migration

There is no Alembic in this project: `_migrate_schema()` in `app/__init__.py`
runs idempotent `ALTER TABLE ADD COLUMN` at boot, and `db.create_all()` creates
new tables. Both new tables and all three new columns are registered there, so
**no manual migration step is required** — start the app and the schema updates
itself.

---

## 8. Tests

* `tests/test_run_writeback.py` — verdict classification, timezone/day-rollover
  handling, and which columns get stamped (including the "do not blank a manual
  label" and "unknown timezone degrades to UTC" cases).
* `tests/test_model_version.py` — version label validation, whitespace
  normalisation, and the broken-operator-pattern fallback.

Both run without a database. The DB-backed paths (row matching, run records, the
collab queue) need PostgreSQL:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/lm_test
pytest tests/
```
