# Univer Sheets — vendored bundle (built by /frontend)

This folder holds the **built** Univer Sheets bundle that upgrades the LAN Test
Matrix editor from the built-in grid to a full spreadsheet UI, fully offline
(no CDN — PRD §16.2).

```
univer.full.umd.js    <- built artifact (assigns window.LMUniver)
univer.full.umd.css   <- built artifact
```

These two files are produced by the Vite project at the repository root:

```bash
cd frontend
npm install     # or use a vendored node_modules / local registry (offline)
npm run build   # emits the two files here
```

See `frontend/README.md` for the full build + air-gapped deployment guide.

## How it activates

- The adapter (`frontend/src/adapter.ts`) is compiled **into** the bundle, so
  no separate `lm-univer-adapter.js` is needed. The bundle sets
  `window.LMUniver = { mount }`.
- `app/static/js/lanmatrix/grid.js` auto-detects `window.LMUniver.mount` and
  uses Univer; otherwise it silently falls back to the built-in grid.
- The editor route only injects the `<script>`/`<link>` tags when
  `univer.full.umd.js` exists here (`_univer_bundle_available()` in
  `app/routes/lanmatrix_pages.py`), so committing this folder empty is safe.

## Data contract (unchanged, server stays authoritative)

- Columns  <- `GET /api/v1/projects/{id}/fields` (respects `is_readonly`,
  `is_required`, `data_type`, `options`, `help_text`).
- Rows     <- `GET /api/v1/projects/{id}/items` (each row carries `id`, `version`).
- Edits    -> `PATCH .../items/{id}` with `{version, changes:{field_key:value}}`;
  a `409 VERSION_CONFLICT` refreshes the cell from `error.details.server_data`.

Do **not** hand-edit the two built files; rebuild from `/frontend` instead.
