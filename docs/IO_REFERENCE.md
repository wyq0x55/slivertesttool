# 入出力参考 (I/O Signal Reference)

A project-level pool of I/O signals, modelled after the **Const 参考** panel:
searchable, one-click copy, add-in-place, and Excel import/export. It lets test
steps reference input / expected signals by a single **名称(路径)** cell instead
of two separate columns.

## Concept

Every project now owns an `io` sheet that acts as a shared **signal pool**. Each
entry has three fields:

| Field      | 中文    | Meaning                                        |
|------------|---------|------------------------------------------------|
| `io_name`  | 名称    | Signal name (unique within the project)        |
| `io_path`  | 路径    | Full access path, e.g. `ECU.Engine.Speed`      |
| `io_note`  | 备考    | Free-text note (optional)                      |

Both `io_name` **and** `io_path` must be unique across the pool — no two entries
may share a name, and no two entries may share a path.

> The path must **not** contain parentheses `()`, because the combined display
> form `名称(路径)` uses them as delimiters.

## Filling in 入力值 / 期待值

Input and expected signals are entered in a **single cell** using the form:

```
名称(路径)
```

For example `EngSpd(ECU.Engine.Speed)`.

Internally the step document still stores the signal as `[name, path]`; the
single-cell form is only the display / entry format. `joinSig` / `splitSig`
convert between the two automatically, so existing exports (`silver_json_export`)
are unaffected.

## 入出力参考 panel

Open the step editor and switch to the **入出力** tab in the reference panel:

- **Search** — filter the pool by name, path, or note.
- **Copy** — click an entry to copy its `名称(路径)` form into the focused cell.
- **+ 新增** — add a new signal inline. The form validates name/path uniqueness
  before it is written. In collaborative mode the row is inserted through the
  shared Y.Doc; otherwise it goes through the REST `pool/io/entries` endpoint.

The same **+ 新增** entry point also exists for **const**, so you can register a
new constant without leaving the editor.

## Excel import / export

The reference panel's import / export dialog now offers **入出力信号池 (名称 /
路径)** alongside Lib and Const.

### Export

Downloads an `.xlsx` with the columns 名称 / 路径 / 备考 (a hidden key row maps
them to `io_name` / `io_path` / `io_note`).

### Import file structure

The importer reads a **flat table driven by a header row** — column order and
position are free, and the header may sit on any of the first 30 rows. Each row
below the header is one signal. Only the header labels matter; they are matched
case-insensitively against these aliases:

| Field key | Accepted header labels                                              | Required |
|-----------|--------------------------------------------------------------------|----------|
| `io_name` | `io_name` · `名称` · `名前` · `信号名` · `信号名称` · `signal` · `signal_name` · `name` | Yes |
| `io_path` | `io_path` · `路径` · `パス` · `path` · `signal_path` · `アクセスパス`     | Recommended |
| `io_note` | `io_note` · `备考` · `備考` · `note` · `notes` · `说明`                 | No |

Minimal example:

| 名称   | 路径              | 備考 |
|--------|-------------------|------|
| EngSpd | ECU.Engine.Speed  | rpm  |
| VehSpd | ECU.Vehicle.Speed |      |

Notes:

- A `No.` / index column (or any unrecognised column) is ignored.
- The header row must name **at least** `io_name` or `io_path`; otherwise the
  sheet is skipped.
- Every worksheet in the workbook is scanned, so multiple sheets are merged.
- A file exported from this tool re-imports losslessly.

### Import rules

Upload an `.xlsx` with matching headers. Import rules:

- Rows with an empty `io_name` are skipped.
- Upsert is keyed on `io_name`: an existing signal with the same name is updated
  in place; otherwise a new one is inserted.
- Path uniqueness is enforced — if a row's `io_path` is already claimed by a
  **different** signal the row is rejected and reported, but the rest of the
  import still proceeds (row-level errors do not abort the whole import).

## Extract from 手順 (VHILS) — 从手順抽取入出力

Instead of authoring the pool by hand, you can **harvest** it from the step
procedures you already wrote. Every `lib` (VHILS subroutine) and `test` row
stores its procedure as a step document whose body declares the signals it uses
in `input_signals` / `expected_signals` (each a `[name, path]` pair). The
extractor collects those declarations, de-duplicates them, and merges the result
into the `io` pool.

### How to use

Open the **导入 Excel** dialog and pick format **入出力信号池 (名称 / 路径)**. An
extract panel appears below the file picker:

- Choose the source procedures: **Lib 手順** (default) and/or **测试手順**.
- Pick a **模式** (mode) — the same upsert / insert-only / replace-all selector
  the file import uses.
- Click **从手順抽取入出力**. No file upload is needed.

### Behaviour

- **De-duplication** is on `(name, path)`, case-insensitive — the same signal
  used across many steps or both sheets is added once.
- **Uniqueness** is enforced exactly like an Excel import: if two *different*
  signals collide on name or path they are reported as per-row conflicts, and
  the rest still merge (a conflict never aborts the whole extraction).
- Declarations that carry **only a path (no name)** cannot key the name-unique
  pool and are counted as *skipped* rather than added.
- `io_note` is never written by extraction, so an upsert onto an existing pool
  entry keeps its note.

The result summary reports: rows scanned, distinct signals found, created /
updated counts, and any name/path conflicts.

> REST: `POST /api/v1/projects/<id>/io/extract` with JSON
> `{"sheets": ["lib", "test"], "mode": "upsert"}`.

## Build note

The core I/O reference feature (panel, single-cell entry, import/export) works
out of the box on the Python + JS backend. The **Vite frontend** step adapter
(`frontend/src/steps_adapter.ts`) was also updated to the single-column layout —
run `npm run build` in `frontend/` to regenerate the bundled editor if you use
the Vite build.
