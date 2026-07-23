# Test Matrix (Excel Import / Export)

The **Test Matrix** feature lets the platform import an Excel *test-requirement*
workbook, store it in the database as an online-editable test summary table plus
its test items, and export the **same Excel layout** back out. It is fully
**additive**: the existing Upload → queue → execute → report flow is unchanged.

It is designed around the reference workbook
`VHILS1_RWS_MCU_Qualification_Test_SYS.xlsx` and follows `lan_test_matrix_prd.md`.

---

## 1. Concepts

| Excel concept | Model | Table |
|---------------|-------|-------|
| Summary sheet `4.TestRequirement` (the `DB` table) | `TestMatrix` | `test_matrices` |
| One summary row + its procedure block | `TestItem` | `test_matrix_items` |

* **`TestMatrix`** — one imported workbook. Holds the summary-sheet metadata:
  display `name`, `source_filename`, the `summary_sheet` name, and the literal
  `id_prefix` used by the summary sheet's calculated `テストID` column.
* **`TestItem`** — one test item. Carries the 20 summary columns
  (`テスト区分` … `上位要求ID`) as first-class fields, plus the per-item
  procedure block (steps and input/expected signal headers) stored as JSON in
  `steps_json`.

Keeping both sides in one relational model is what guarantees the two Excel
representations (the summary `DB` table and the per-category detail sheets) stay
**consistent** — they are regenerated together on export.

### The `テストID` round trip

The summary sheet computes `テストID` with:

```
="<prefix>"&TEXT(テスト区分,"000")&TEXT(テスト番号,"000")
```

We store only the `id_prefix`, `category`, and `test_no`; `TestItem.test_id`
reconstructs the exact value on demand. Detail-sheet procedure blocks are matched
to summary items by the `(category, test_no)` pair parsed from the block's
literal test id, so matching is independent of the prefix and survives
re-export.

---

## 2. Data flow

```
                       parse_workbook()                      build_workbook()
 .xlsx  ────────────▶  (openpyxl, no Flask)  ──▶  DB rows  ──▶  (openpyxl)  ──▶  .xlsx
        import                                   TestMatrix                    export
                                                 + TestItem[]
```

* `app/services/lanmatrix/matrix_excel.py` — **Flask-independent** codec. Single source of
  truth for the schema (`SUMMARY_COLUMNS`, `DETAIL_LABELS`) and the detail-block
  layout. Public API:
  * `parse_workbook(source, *, source_filename="") -> dict`
  * `build_workbook(matrix: dict) -> openpyxl.Workbook`
  * `MatrixExcelError`
* `app/services/matrix_service.py` — persistence bridge: `import_workbook`,
  `export_workbook`, `list_matrices`, `get_by_key`, `delete_matrix`,
  `next_matrix_key` (`M{id:06d}`).

Only items that have a procedure block get a detail sheet block on export;
summary-only items (e.g. priority `不要` / result `-`) are written to the summary
`DB` table only — matching the source workbook.

---

## 3. REST API

Base blueprint: `matrix_bp` at `/api/matrices`.

| Method & path | Purpose |
|---------------|---------|
| `GET /api/matrices` | List imported matrices. |
| `POST /api/matrices` | Create a **blank project** (JSON/form: optional `name`, `submitter`). |
| `POST /api/matrices/import` | Import an `.xlsx` (multipart form: `file`, optional `submitter`, `name`) → new matrix. |
| `POST /api/matrices/<key>/import` | Import an `.xlsx` **into an existing project** (multipart form: `file`, optional `replace` = `1`/`0`). |
| `GET /api/matrices/<key>` | Matrix detail (metadata + items with steps). |
| `GET /api/matrices/<key>/items` | Matrix items (summary rows). |
| `GET /api/matrices/<key>/export` | Download the regenerated `.xlsx`. |
| `DELETE /api/matrices/<key>` | Delete a matrix and its items. |

Only `.xlsx` uploads are accepted. Parse failures return HTTP 400 with a JSON
`error` message.

### Two ways to start a project

1. **Blank project, then import** — `POST /api/matrices` creates an empty
   project; open it and `POST /api/matrices/<key>/import` parses a workbook into
   it. With `replace=1` (default) the parsed items replace any current items; the
   project's id prefix, summary-sheet name and (if still unnamed) display name are
   refreshed from the workbook.
2. **Import as a new project** — `POST /api/matrices/import` parses a workbook and
   creates a populated project in one step.

Uploaded streams are buffered defensively before parsing, so non-seekable upload
objects (Werkzeug's `SpooledTemporaryFile`) are handled correctly.

### Pages

| Path | Page |
|------|------|
| `/matrices` | Create-blank-project form, import-as-new-project form, and the matrix list (Export / Delete actions). |
| `/matrices/<key>` | Import-into-project control, summary table, and each item's metadata and procedure steps. |

A **Test Matrix** link is added to the top navigation.

---

## 4. Storage

Two new SQLAlchemy tables are created automatically by `db.create_all()` on
startup — no manual migration is required:

* `test_matrices`
* `test_matrix_items` (FK → `test_matrices.id`, `ON DELETE CASCADE`)

---

## 5. Tests

`tests/test_matrix_excel.py` runs on **openpyxl + stdlib `unittest` only** (no
Flask needed):

```bash
python -m unittest tests.test_matrix_excel
```

It covers reference parsing, a lossless parse → build → reparse round trip,
summary-only export behaviour, and the missing-`DB`-table error path. Place a
copy of the reference workbook at `tests/data/` (or `/app/uploads/`) to enable
the round-trip cases; they self-skip when it is absent.
