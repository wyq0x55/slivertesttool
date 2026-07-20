# Univer Sheets frontend (Vite)

This Vite project bundles **Univer Sheets** (Apache-2.0) plus a thin adapter
into a single offline asset that upgrades the LAN Test Matrix editor
("测试手顺构建表" / test-procedure construction table) from the built-in grid to a
full spreadsheet UI — **without any backend or API change**.

```
frontend/
├── package.json         # Univer preset deps + Vite
├── vite.config.ts       # library build -> app/static/vendor/univer/
├── tsconfig.json
└── src/
    ├── main.ts          # entry: sets window.LMUniver = { mount }
    └── adapter.ts       # UniverGridAdapter -> implements the grid.js contract
```

## How it plugs in

`app/static/js/lanmatrix/grid.js` already contains an engine-abstraction:

```js
LMGrid.create(opts)  // uses window.LMUniver.mount(opts) if present,
                     // else falls back to the built-in offline grid.
```

`editor.js` calls `setFields / setData / getSelectedIds / clearSelection`
and (after row moves) `_setRowSelected / _syncSelectAll / _emitSelection`.
`UniverGridAdapter` implements all of them and routes every cell edit through
`opts.onSave(...)` → `PATCH /api/v1/projects/{id}/items/{id}`, so the server
stays the single source of authority (validation, permissions, optimistic
lock, audit).

The Flask editor route only emits the `<script>`/`<link>` tags when the built
bundle exists (`_univer_bundle_available()`), so shipping without it is safe.

## Build (on a machine WITH internet / a build box)

```bash
cd frontend
npm install          # resolves @univerjs/presets + preset-sheets-core + vite
npm run build        # emits app/static/vendor/univer/univer.full.umd.{js,css}
```

Restart the Flask app. Open a project → the editor status bar shows
`· Univer 引擎` instead of `· 内置表格`.

## Offline build / air-gapped deployment

No internet on the target LAN? Two supported paths:

1. **Vendor `node_modules`** — on a build box run `npm install`, then copy the
   whole `frontend/` (including `node_modules/`) to the offline machine and run
   `npm run build` there (Vite/TypeScript run fully offline once installed).

2. **Pre-build the bundle** — run `npm run build` on the build box and ship only
   the two output files:
   ```
   app/static/vendor/univer/univer.full.umd.js
   app/static/vendor/univer/univer.full.umd.css
   ```
   These are static and self-contained (no CDN, PRD §16.2 compliant). Dropping
   them into an existing deployment is enough — no Node needed on the server.

Pin exact versions in `package.json` for reproducible offline builds; the
caret ranges here are a starting point — replace with the versions your build
box resolved (`npm ls @univerjs/presets`).

## Localization & feature notes (zh-CN build)

- **Language packs — Simplified Chinese (zh-CN):** every preset (current and
  future) is registered with its `.../locales/zh-CN` bundle and the workbook is
  created with `LocaleType.ZH_CN`. When adding a new preset/plugin, import its
  `zh-CN` locale and push it into the `locales` array — never `en-US`.
- **Step detail = tables, not a dialog:** the main grid no longer auto-opens a
  modal step editor when a `steps` cell is selected (`_maybeOpenSteps` is a
  deliberate no-op). The 入力値 / 期待値 / 手順 regions are rendered as
  first-class Univer tables through **`@univerjs/preset-sheets-table`**
  (`steps_adapter.ts`).
- **Crosshair highlight:** **`@univerjs/sheets-crosshair-highlight`** is
  registered as a standalone plugin and enabled via its facade
  (`univerAPI.setCrosshairHighlightEnabled(true)`), highlighting the active
  cell's row + column in both the matrix and the step view.

Both new packages are pinned to `0.21.5` (same line as the presets) in
`package.json`. Run `npm install` again after pulling these changes, then
`npm run build` to regenerate `app/static/vendor/univer/univer.full.umd.{js,css}`.

## Univer 0.6.10 → 0.21.5 upgrade notes

The whole `@univerjs/*` stack was bumped from `0.6.10` to `0.21.5`
(`dependencies` + `overrides`). Two source-level API changes were required:

- **`merge` → `mergeLocales`.** `@univerjs/presets` no longer exports the
  lodash-style `merge`; locale bundles are combined with `mergeLocales(...)`,
  which takes the locale objects directly (no `{}` accumulator first arg).
  Applied in `main.ts`, `adapter.ts`, `steps_adapter.ts`.
- **`@univerjs/themes`** now backs `defaultTheme` (still re-exported by
  `@univerjs/presets`, so the import is unchanged) and is pinned in `overrides`
  to keep the graph on a single version.

Everything else — the `createUniver({ locale, locales, theme, presets })`
shape, the preset list, the standalone crosshair-highlight plugin + its facade
import, and the `window.LMUniver` / `window.LMUniverSteps` contract — is
unchanged; the adapters already probe every facade method defensively, so the
grid.js fallback keeps the frontend non-breaking.

## Troubleshooting: `"MAX_COLUMN_COUNT" is not exported by @univerjs/core`

Symptom (Rollup/Vite build error):

```
node_modules/@univerjs/sheets-table/node_modules/@univerjs/engine-formula/lib/es/index.js
  "MAX_COLUMN_COUNT" is not exported by "node_modules/@univerjs/core/lib/es/index.js"
```

Cause: a **Univer version mismatch**. Adding `@univerjs/preset-sheets-table`
pulled *newer* transitive engines (note the **nested**
`sheets-table/node_modules/@univerjs/engine-formula` and
`sheets-table-ui/node_modules/@univerjs/design` in the tree). Those newer
engines expect core APIs (e.g. `MAX_COLUMN_COUNT`) that a mismatched
`@univerjs/core` does not export. The whole `@univerjs/*` graph must be **one**
version.

Fix (already applied here): `package.json` → `overrides` now pins the **entire**
`@univerjs/*` stack — core, engine-formula, engine-render, design, ui, docs,
sheets, sheets-ui, sheets-table(-ui), sheets-crosshair-highlight, … — to
`0.21.5`. A top-level npm `override` rewrites every nested copy too.

Because npm caches the old resolution in the lockfile, re-resolve from scratch:

```bash
cd frontend
rm -rf node_modules package-lock.json    # PowerShell: Remove-Item -Recurse -Force node_modules, package-lock.json
npm install
npm run build
# sanity check: every line must read 0.21.5 (no "deduped to" another version)
npm ls @univerjs/core @univerjs/engine-formula @univerjs/design @univerjs/sheets-table
```

If `npm install` ever reports `ETARGET` / "No matching version found for
@univerjs/<pkg>@0.21.5", that sub-package has no `0.21.5` release — delete just
that one line from `overrides` and reinstall. If you instead want a different
release, upgrade **all** `@univerjs/*` deps + overrides together to one coherent
version (e.g. the latest `0.x`) rather than mixing lines.

## Smoke test after building

1. Editor loads, status bar reads `· Univer 引擎`.
2. Header row shows all active fields; `*` = required, 🔒 = read-only.
3. Edit a text cell → blur → cell shows a brief save, value persists on reload.
4. Change a `single_select` / `boolean` cell via its dropdown → persists.
5. Editing a read-only (🔒) cell reverts.
6. Select rows → toolbar "复制所选/删除所选/上移/下移" act on the selection.
7. Trigger a version conflict (edit same row from two tabs) → toast + refresh.
