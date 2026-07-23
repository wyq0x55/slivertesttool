# Real-time collaboration — vendored bundle (built by /frontend)

This folder holds the **built** Yjs collaboration runtime that powers the
optional real-time (multi-user) editing of the Test Matrix editor, fully
offline (no CDN).

```
collab.umd.js    <- built artifact (assigns window.LMCollab = { Y, WebsocketProvider, Awareness })
```

It is produced by the Vite project at the repository root:

```bash
cd frontend
npm install       # or use a vendored node_modules / local registry (offline)
npm run build     # builds BOTH the Univer bundle and this collab bundle
# or just this one:
npm run build:collab
```

## How it is used

`app/static/js/lanmatrix/collab.js` (a hand-written, build-free controller)
auto-detects `window.LMCollab`. When present AND the page was rendered with
`collab_available = true` (see `app/routes/lanmatrix_pages.py`), the editor
connects to the Python collab WebSocket server (`run_collab.py`, default
`ws://<host>:1234`) and drives all edits through the shared `Y.Doc`.

If this file is absent, `window.LMCollab` is undefined and the editor silently
stays in its classic REST + polling mode — shipping this bundle is always
optional and non-breaking.

## Runtime server

The browser bundle is only half of the feature. Start the Python side too:

```bash
python run_collab.py           # serves ws://0.0.0.0:1234
```

See the design docs (`yjs-collab-*`) for the full architecture.
