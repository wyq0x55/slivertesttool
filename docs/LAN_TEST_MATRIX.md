# LAN Test Matrix — Online Editing Platform

A fully **offline, LAN-deployable** online editor for test matrices, added on top
of the Silver Test Platform. It keeps the original upload-and-execute flow intact
and adds a spreadsheet-style editor with dynamic columns, cell restrictions,
batch search/replace with preview & undo, RBAC, audit, optimistic locking,
Excel import/export and backup/restore.

Governing spec: `lan_test_matrix_prd.md`. This document is the implementation
reference (architecture, data model, API, test matrix, acceptance mapping, AI
development guide and offline deployment guide).

---

## 1. Technology & licensing

| Concern            | Choice                                             | License |
|--------------------|----------------------------------------------------|---------|
| Web framework      | Flask 3 (existing app factory)                     | BSD |
| ORM                | SQLAlchemy 2                                        | MIT |
| Database           | **PostgreSQL** (only)                              | PostgreSQL |
| Task queue         | **Huey** on PostgreSQL (`huey.contrib.sql_huey`)   | MIT |
| Excel I/O          | **openpyxl**                                        | MIT |
| Grid (frontend)    | Built-in editable grid, optional **Univer Sheets** | Bundled / Apache-2.0 |
| Password hashing   | Werkzeug PBKDF2 (Argon2/bcrypt drop-in ready)      | BSD |

No component requires a commercial license, and **nothing is loaded from a public
CDN** — all CSS/JS is served from local `static/`. This satisfies the PRD §16.2
release blockers.

The editing surface ships a dependency-free grid so the platform is usable
offline out of the box. To upgrade to Univer Sheets, vendor its Apache-2.0 UMD
bundle under `app/static/vendor/univer/` and add the adapter described in
`app/static/vendor/univer/README.md`; `LMGrid.create()` auto-detects it and the
whole data contract (fields, rows, optimistic-lock saves) stays identical.

---

## 2. Package layout

```
app/lanmatrix/
  __init__.py      register(app): create lm_* tables, seed admin, mount blueprints
  fields.py        DATA_TYPES, SYSTEM_FIELDS, coerce_value()          (pure)
  security.py      formula-injection, control chars, bounded regex    (pure)
  validation.py    FieldSpec, validate_value/record, CAN/timeout rules(pure)
  batch.py         batch operation engine (set/replace/regex/…)       (pure)
  excel_io.py      openpyxl template / import / export                (openpyxl)
  models.py        8 SQLAlchemy entities, lm_* tables
  permissions.py   RBAC capability matrix
  audit.py         audit writer with secret redaction
  repository.py    sort/filter whitelisting
  service.py       orchestration (projects, fields, items, batch, comments)
  excel_service.py import preview/commit + export jobs
  api.py           /api/v1 REST blueprint (unified envelope, CSRF)
  pages.py         /lanmatrix HTML pages
app/static/js/lanmatrix/    api.js, grid.js, editor.js, projects.js, fields.js
app/static/css/lanmatrix.css
app/templates/lanmatrix/    login, projects, editor, fields, audit
app/static/vendor/univer/   offline Univer drop-in instructions
scripts/lm_backup.py, scripts/lm_restore.py
tests/test_lanmatrix_*.py
```

The `pure` modules import only stdlib (+ openpyxl for `excel_io`) so they are
unit-tested without Flask via `tests/lm_helpers.load()`.

---

## 3. Data model (`lm_*` tables)

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `lm_users` | accounts | `username`, `password_hash`, `is_system_admin`, `failed_logins`, `locked_until` |
| `lm_project_members` | per-project role | `project_id`, `user_id`, `role` |
| `lm_projects` | matrix project | `code` (unique), `name`, `status`, `owner_id` |
| `lm_field_definitions` | dynamic columns | `field_key`, `data_type`, `is_required/readonly`, `validation_rule`, `option_source` |
| `lm_test_items` | rows | system columns + `custom_values` (JSONB), `version`, `deleted_at` |
| `lm_cell_comments` | cell comments | `test_item_id`, `field_key`, `content` |
| `lm_audit_logs` | audit trail | `action`, `old_value`, `new_value`, `batch_id`, `request_id` |
| `lm_data_jobs` | import/export jobs | `job_type`, `status`, `preview`, counts |

The test-execution `tasks` table (shared with the original platform) is linked
to lanmatrix by two nullable, indexed columns: `project_id` (owning project) and
`submitter_id` (authenticated `lm_users.id`). Only members of `project_id` may
view / run / download a task; rows left `NULL` are legacy/unscoped and surface
only in the system-admin console.

Custom field values live in `lm_test_items.custom_values`, a portable JSON column
that maps to **JSONB on PostgreSQL** (`JSON().with_variant(JSONB, "postgresql")`)
and plain JSON/TEXT elsewhere. High-frequency core fields (case_id, title,
result, workflow_status, …) are first-class columns and indexed.

Optimistic locking: every row carries an integer `version`. Updates must send the
client's `version`; a mismatch raises `VERSION_CONFLICT` (HTTP 409).

---

## 4. Roles & permissions (RBAC)

| Capability | system_admin | project_admin | editor | reviewer | reader |
|-----------|:---:|:---:|:---:|:---:|:---:|
| view project/items | ✓ | ✓ | ✓ | ✓ | ✓ |
| create/edit/delete items | ✓ | ✓ | ✓ | | |
| batch (selection) | ✓ | ✓ | ✓ | | |
| batch **all rows** | ✓ | ✓ | | | |
| manage fields | ✓ | ✓ | | | |
| import | ✓ | ✓ | ✓ | | |
| import **replace_all** | ✓ | ✓ | | | |
| export | ✓ | ✓ | ✓ | ✓ | ✓ |
| add comment | ✓ | ✓ | ✓ | ✓ | |
| view audit | ✓ | ✓ | | | |
| manage members | ✓ | ✓ | | | |
| task view / upload(run) / cancel / download | ✓ | ✓ | ✓ | ✓ | ✓ |
| task delete | ✓ | ✓ | | | |

Upload Tasks are **members-only**: membership itself is the gate, so any of the
four project roles may view/upload/cancel/download the project's tasks; only a
project_admin may delete. System administrators additionally get a global,
session-gated console (`/lanmatrix/admin`) — no `ADMIN_TOKEN`.

Permissions are enforced **server-side** in `api.py` via `permissions.require()`.
The UI only hides controls; hiding is never the security boundary.

---

## 5. API reference (`/api/v1`)

All responses use the unified envelope:

```json
{ "success": true, "data": {…}, "error": null, "request_id": "req-abc123" }
```

Errors: `success:false`, `data:null`, `error:{code,message,details}` with an
appropriate HTTP status. Optimistic-lock conflict:

```
HTTP 409  error.code = "VERSION_CONFLICT"
error.details = { client_version, server_version, server_data }
```

### Auth
| Method | Path | Notes |
|-------|------|-------|
| POST | `/auth/login` | body `{username,password}` → `{user, csrf_token}`; sets session cookie. Account locks after 5 failures for 15 min. |
| POST | `/auth/logout` | clears session |
| GET  | `/auth/me` | current user + csrf token |

State-changing requests must send header `X-CSRF-Token` equal to the login
`csrf_token` (double-submit, constant-time compared).

### Projects / fields / items
| Method | Path |
|-------|------|
| GET/POST | `/projects`, `/projects` (create blank) |
| GET/PATCH/DELETE | `/projects/{id}` |
| GET/POST | `/projects/{id}/fields` |
| PATCH | `/projects/{id}/fields/{fid}` |
| GET | `/projects/{id}/items` — `page,page_size,sort,q,filter,combinator` |
| POST | `/projects/{id}/items` — `{values, [anchor_id], [place]}` (`place`=`above`/`below` 定位插入) |
| PATCH | `/projects/{id}/items/{iid}` — `{version, changes}` (optimistic lock) |
| DELETE | `/projects/{id}/items/{iid}` (soft delete) |
| POST | `/projects/{id}/items/{iid}/duplicate` · `/restore` |
| POST | `/projects/{id}/items/bulk-delete` — `{ids}` → `{deleted}` |
| POST | `/projects/{id}/items/bulk-duplicate` — `{ids}` → `{items, created}` |
| POST | `/projects/{id}/items/move` — `{ids, direction}` (`up`/`down`，整块移动并归一化 `row_order`) |

### Batch
| Method | Path | Body |
|-------|------|------|
| POST | `/projects/{id}/items/batch-preview` | `{field_key, operation, scope}` |
| POST | `/projects/{id}/items/batch-update` | same → `{batch_id, changed}` |
| POST | `/projects/{id}/items/batch-undo` | `{batch_id}` → `{restored}` |

### Database administration (system_admin only)
| Method | Path | Body / Result |
|-------|------|------|
| GET | `/admin/db/overview` | → `{backend, version, database, db_user, server_addr, size_pretty, tables:[{schema,name,est_rows,total_bytes,size_pretty}]}` |
| POST | `/admin/db/query` | `{sql, read_only}` → `{columns, rows, rowcount, returns_rows, truncated, command, elapsed_ms, read_only}` |
| GET | `/admin/db/tables` | → `{tables:[{name, est_rows}]}` |
| GET | `/admin/db/tables/<table>/schema` | → `{table, columns:[{name,data_type,udt_name,nullable,default,max_length,is_identity,auto,is_pk}], primary_key:[…]}` |
| GET | `/admin/db/tables/<table>/rows` | query `?page&page_size&order_by&desc` → `{table, columns, rows, total, page, page_size, pages, primary_key}` |
| POST | `/admin/db/tables/<table>/rows` | `{values:{col:val,…}}` → `{row}` (201) |
| PATCH | `/admin/db/tables/<table>/rows` | `{pk:{…}, changes:{…}}` → `{row}` |
| DELETE | `/admin/db/tables/<table>/rows` | `{pk:{…}}` → `{deleted}` |

All endpoints are gated server-side to `is_system_admin` users. Read-only mode
allows only `SELECT/WITH/SHOW/EXPLAIN/TABLE/VALUES` and rolls the statement back;
write mode commits in its own transaction. Page: `/lanmatrix/admin/db`.

**No-SQL table CRUD.** Table and column identifiers are validated against live
catalog introspection (whitelist) before being quoted, and every value is passed
as a bound parameter — safe from injection. `page_size` is capped at 500 (default
50). `insert/update/delete` operate through the table's **primary key**; tables
without a PK (`kv`, `schedule`, `task`, …) support browse only — form edit/delete
is disabled and users should fall back to the SQL console.

`operation` = `{op, value|find|replace|pattern}`; ops: `set, clear, prefix,
suffix, find_replace, regex_replace, increment, decrement, status_transition,
multi_add, multi_remove`. `scope` = `{type:"ids"|"filter"|"all", …}`. `all`
requires the `item.batch_all` capability. Batch update is a **single
transaction** — any invalid row rolls back the whole batch.

### Members (project.members; project_admin / system_admin)
| Method | Path | Body / Result |
|-------|------|------|
| GET | `/projects/{id}/members` | → `{members:[{id,user_id,username,display_name,role}], roles:[…]}` (any member) |
| GET | `/projects/{id}/members/candidates` | query `?q` → `{candidates:[{id,username,display_name}]}` (excludes existing members) |
| POST | `/projects/{id}/members` | `{user_id\|username, role}` → `{member}` (201) |
| PATCH | `/projects/{id}/members/{member_id}` | `{role}` → `{member}` (keeps ≥1 project_admin) |
| DELETE | `/projects/{id}/members/{member_id}` | → `{removed}` (keeps ≥1 project_admin) |

### Upload Tasks — per project, members only (task.*)
| Method | Path | Body / Result |
|-------|------|------|
| GET | `/projects/{id}/tasks` | → `{tasks:[…], models:[…], license:{…,queued_jobs}, role, can_delete}` (task.view) |
| POST | `/projects/{id}/tasks/upload-tree` | multipart `files/paths`,`lib_files/lib_paths`,`stdlib_files/stdlib_paths`,`test_ids`,`model`,`folder_name` → `{created,duplicates,errors,notes}` (task.upload; submitter = account) |
| GET | `/projects/{id}/tasks/{key}` | → `{task}` |
| GET | `/projects/{id}/tasks/{key}/detail` | → `{task:{…,events:[…]}}` |
| GET | `/projects/{id}/tasks/{key}/stream` | SSE (`progress`/`status`/`result`/`log`, `end`) |
| GET | `/projects/{id}/tasks/{key}/jdgrslt` | → `{available,verdict,failed_steps,content}` |
| POST | `/projects/{id}/tasks/{key}/cancel` | → `{task_id,result,message}` (task.cancel) |
| DELETE | `/projects/{id}/tasks/{key}` | → `{deleted}` (task.delete = project_admin) |
| GET | `/projects/{id}/tasks/{key}/download` | report `.zip` (task.download) |

Only members of `{id}` reach these (RBAC `task.*`); non-members get 403. Tasks
carry `project_id` + `submitter_id`; legacy rows with `project_id = NULL` are
visible only in the admin console, never in a project's list.

### Admin console (system_admin only; session, no ADMIN_TOKEN)
| Method | Path | Body / Result |
|-------|------|------|
| GET | `/admin/users` | → `{users:[{…,is_active,project_count}]}` |
| POST | `/admin/users` | `{username,password,display_name,email,is_system_admin,status}` → `{user}` (201) |
| PATCH | `/admin/users/{id}` | `{changes:{display_name?,email?,status?,is_system_admin?,password?}}` → `{user}` |
| DELETE | `/admin/users/{id}` | → `{deleted}` (guards last admin / self) |
| GET/POST/DELETE | `/admin/models` | list / `{name,path}` add / `{name}` remove |
| POST | `/admin/models/bulk` | `{models:[…]}` replace all |
| GET/POST | `/admin/license` | status / `{count}` set concurrency |
| GET | `/admin/tasks` | → `{tasks:[{…,project_code}]}` (all projects + legacy) |
| POST | `/admin/tasks/{key}/cancel` · DELETE | cancel / delete any task |

Guards: cannot disable/demote/delete the **last active system admin**, nor
delete the currently logged-in account. Page: `/lanmatrix/admin`.

### Excel
| Method | Path | Notes |
|-------|------|-------|
| GET | `/projects/{id}/excel/template` | download `.xlsx` template (hidden field-key row) |
| POST | `/projects/{id}/imports` | multipart `file`,`mode`; returns a **preview** job (no writes) |
| GET | `/imports/{job}` | fetch job + preview |
| POST | `/imports/{job}/commit` | persist previewed rows in one transaction |
| POST | `/projects/{id}/exports` | `{columns?, item_ids?}` → `.xlsx` download |

Import modes: `insert_only`, `upsert`, `update_only`, `replace_all`
(replace_all needs `import.replace`). Import always previews first; commit is
blocked when the preview has blocking errors (except `replace_all`).

### Audit / health
| Method | Path |
|-------|------|
| GET | `/projects/{id}/audit-logs` — `page,page_size` |
| GET | `/health` — `{web, database, version}` |

---

## 6. Security controls

- **Formula injection**: imported cells are stripped of control chars and never
  trusted as formulas; exported text starting with `= + - @ TAB CR` is guarded
  with a leading apostrophe (`security.escape_formula`).
- **Bounded regex**: user regex capped at 200 chars, compiled safely, executed
  under a SIGALRM wall-clock timeout (`security.compile_user_regex` /
  `match_with_timeout`) to bound catastrophic backtracking.
- **Control characters** forbidden in stored values (tabs/newlines allowed for
  multiline).
- **Sessions**: HttpOnly + SameSite=Lax cookies; `SESSION_COOKIE_SECURE=1` when
  behind TLS; 12-hour lifetime.
- **CSRF**: double-submit token on every mutating request.
- **Audit redaction**: password/secret/token keys never persisted to audit rows.
- **SQL safety**: sort/filter fields are whitelisted (`repository.SORTABLE` /
  `FILTER_OPS`); all queries are ORM-parameterised.
- **Upload validation**: only `.xlsx`, size-capped by `MAX_UPLOAD_BYTES`.

---

## 7. Backup & restore (PRD §12)

```bash
# Nightly backup (PostgreSQL pg_dump, custom format, from DATABASE_URL)
python scripts/lm_backup.py --out-dir /var/backups/lanmatrix --keep 30

# Restore (stop web + worker first)
python scripts/lm_restore.py /var/backups/lanmatrix/lanmatrix_pg_20250101_020000.dump
```

Backup uses `pg_dump --format=custom`; restore uses
`pg_restore --clean --if-exists`. Both run fully offline on the LAN host and read
the PostgreSQL DSN from `DATABASE_URL`.

---

## 8. Offline deployment

1. **Database** — provision PostgreSQL on the LAN and export (required — SQLite
   is no longer supported):
   `export DATABASE_URL=postgresql+psycopg2://user:pass@dbhost:5432/silvetestapp`
   The Huey task queue reuses this database by default (override with
   `HUEY_DATABASE_URL`); install `psycopg2-binary` and `peewee`.
2. **Secrets** — set `SECRET_KEY`, and `LM_ADMIN_USER` / `LM_ADMIN_PASSWORD` to
   seed the bootstrap administrator (default `admin` / `Admin@12345`, flagged
   *must change password* until you set `LM_ADMIN_PASSWORD`).
3. **Start** — `python run_web.py` (Waitress). Tables `lm_*` and the Huey queue
   tables are created on first boot and the admin is seeded automatically.
4. **Open** — navigate to `/lanmatrix` (also linked as *Matrix Editor* in the
   top nav), log in, create a project or import an Excel file. LAN users can
   self-register via the *注册新账号* link on the login page (see §8.2).
5. **(Optional) Univer** — vendor the bundle per
   `app/static/vendor/univer/README.md`.

Everything is served from local static files; no internet access is required at
runtime.

### 8.1 Configuration (`.env`)

The LAN Test Matrix is now expanded into the platform service layer
(`app/services/lanmatrix`) and is fully driven by the shared `.env` file
(centralised in `app/config.py::Config`, exposed to the pure service modules via
`app/services/lanmatrix/settings.py`). Copy `.env.example` to `.env` and adjust.
All values are optional — the defaults below reproduce the historical behaviour.

| Variable | Default | Purpose |
|----------|---------|---------|
| `LM_ADMIN_USER` | `admin` | Bootstrap system administrator username. |
| `LM_ADMIN_PASSWORD` | *(empty)* | Admin password; empty ⇒ use `Admin@12345` and force a change on first login. |
| `LM_LOCK_THRESHOLD` | `5` | Failed logins before an account is temporarily locked. |
| `LM_LOCK_MINUTES` | `15` | Lockout duration in minutes. |
| `LM_PAGE_SIZE` | `100` | Default server-side page size for item/audit listings. |
| `LM_PAGE_SIZE_MAX` | `500` | Hard cap on page size / batch scope resolution. |
| `LM_BATCH_SAMPLE_LIMIT` | `100` | Max sample rows in a batch search/replace preview. |
| `LM_IMPORT_ERROR_LIMIT` | `500` | Max import-validation errors echoed to the client. |
| `LM_REGEX_MAX_LEN` | `200` | Length cap for user-supplied regexes. |
| `LM_REGEX_TIMEOUT` | `0.25` | Per-match regex wall-clock timeout (seconds). |
| `LM_FILENAME_MAX_LEN` | `120` | Max length of a sanitised upload/export filename base. |
| `LM_TM_ID_PREFIX` | `ID;;` | Test-Matrix (Excel) bridge ID column prefix. |
| `LM_TM_SUMMARY_SHEET` | `4.TestRequirement` | Test-Matrix summary sheet name. |
| `LM_ALLOW_REGISTRATION` | `1` | Enable LAN user self-registration. |
| `LM_REGISTRATION_DEFAULT_STATUS` | `active` | New-account status: `active` (login immediately) or `disabled` (admin approval). |
| `LM_PASSWORD_MIN_LEN` | `8` | Minimum length for a registration / self-set password. |
| `LM_USERNAME_PATTERN` | `^[A-Za-z0-9_.\-]{3,64}$` | Whitelist a chosen username must fully match. |

Database / queue configuration (`DATABASE_URL`, `HUEY_DATABASE_URL`, `HUEY_NAME`)
lives in the same `.env`; see §2 and §8. Changing any value requires a process
restart (values are read once at start-up), matching how the rest of the platform
treats its configuration.

### 8.2 Self-service registration (LAN users)

When `LM_ALLOW_REGISTRATION` is on, the login page shows a **注册新账号** link to
`/lanmatrix/register`. The form collects a username, optional display name/email,
and a password (confirmed twice, ≥ `LM_PASSWORD_MIN_LEN`). On submit it calls
`POST /api/v1/auth/register`, which:

* validates the username against `LM_USERNAME_PATTERN`, enforces the password
  length and rejects duplicates;
* creates a **plain** account — never a system admin — with **no** project
  membership, so it can authenticate but sees no projects until an administrator
  assigns it a project role (a safe default for an internal-network tool);
* if `LM_REGISTRATION_DEFAULT_STATUS=active`, immediately establishes the session
  (returns a CSRF token, mirroring login) and the browser lands on the projects
  page; if `disabled`, the account is created *pending* and the user is told to
  wait for an administrator to activate it (set `status=active`).

The register endpoints (`lanmatrix_pages.register`, `lanmatrix_api.register`) are
whitelisted in the global login gate and CSRF-exempt (like login), since no
session exists yet.

---

## 9. Test matrix

| # | Area | Test | File |
|---|------|------|------|
| T-01 | Coercion | integer/decimal/hex/bool/date/multi-select + errors | `test_lanmatrix_fields.py` |
| T-02 | System fields | required keys present, readonly bookkeeping | `test_lanmatrix_fields.py` |
| T-03 | Formula injection | detect/escape/sanitize, control chars | `test_lanmatrix_security.py` |
| T-04 | Filenames | path stripping, unicode, default | `test_lanmatrix_security.py` |
| T-05 | Regex bounds | length cap, invalid, timeout wrapper | `test_lanmatrix_security.py` |
| T-06 | Batch validate | unknown/missing/typed operation specs | `test_lanmatrix_batch.py` |
| T-07 | Batch apply | set/clear/prefix/suffix/replace/regex/inc/multi | `test_lanmatrix_batch.py` |
| T-08 | Validation | required/length/range/pattern/enum/unique | `test_lanmatrix_validation.py` |
| T-09 | CAN & timeout | std 0x7FF / ext 0x1FFFFFFF / timeout ranges | `test_lanmatrix_validation.py` |
| T-10 | Cross-field | date_before/after, readonly skip | `test_lanmatrix_validation.py` |
| T-11 | Excel template | hidden field-key row, info sheet | `test_lanmatrix_excel.py` |
| T-12 | Excel round-trip | export→import equality | `test_lanmatrix_excel.py` |
| T-13 | Excel safety | formula escaped, missing-required, non-seekable stream | `test_lanmatrix_excel.py` |

Run: `python -m unittest discover -s tests -p 'test_lanmatrix_*.py'` (51 cases).

---

## 10. Acceptance mapping (PRD §16)

| PRD acceptance item | Where satisfied |
|---|---|
| Fully offline, no CDN | local static only; Univer optional local bundle |
| RBAC server-side | `permissions.py` + `api._project_and_role` |
| Dynamic fields | `field_definitions` + `custom_values` JSONB |
| Front + back validation | JS hints + `validation.py` authoritative |
| xlsx template / import preview / error report / commit | `excel_service.py`, `api` imports/commit |
| Batch preview + audit + undo | `service.batch_*`, `audit.py` |
| Export | `excel_service.export_project` |
| Audit old/new values | `AuditLog.old_value/new_value` |
| Version conflict detection | `update_item` → 409 `VERSION_CONFLICT` |
| Backup + restore | `scripts/lm_backup.py` / `lm_restore.py` |
| Secrets never in logs | `audit._redact` |
| No paid-license deps, no debug in prod | Waitress prod server, MIT/Apache stack |

---

## 11. AI development guide

When extending this platform with an AI assistant, follow these rules so changes
stay safe and consistent:

1. **Keep pure logic pure.** `fields/security/validation/batch/excel_io` must not
   import Flask/SQLAlchemy. Add new rules there and unit-test them via
   `tests/lm_helpers.load()` (no app context needed).
2. **Server is the authority.** Any new mutation must go through `service.py`,
   write an `audit.record(...)`, respect optimistic `version`, and be gated by a
   `permissions` capability in `api.py`. Never trust client-sent role/version.
3. **Extend, don't fork the envelope.** New endpoints return `ok(data)` /
   `err(code,message,details)` and require `X-CSRF-Token` for writes.
4. **Whitelist inputs.** New sortable/filterable fields go into
   `repository.SORTABLE` / `FILTER_OPS`; never interpolate user strings into SQL.
5. **No secrets in audit.** Add sensitive keys to `audit._REDACT_KEYS`.
6. **Offline only.** No new CDN/script/style URLs; vendor assets under
   `static/`.
7. **Migrations are additive.** New tables via `db.create_all()`; new columns via
   an idempotent `ALTER TABLE` step (see `_migrate_schema`) or Alembic.
8. **Test before ship.** `python -m unittest discover -s tests` +
   `python -m py_compile app/lanmatrix/*.py` + `node --check` on changed JS.
