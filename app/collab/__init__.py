"""Real-time collaboration (Yjs / CRDT) package.

Runs OUT OF PROCESS from the Flask/waitress web app (waitress is a synchronous
WSGI server and cannot perform the WebSocket upgrade). ``run_collab.py`` boots
an ASGI app (uvicorn) that serves per-project Yjs rooms; the Flask app only
mints short-lived access tokens (:mod:`app.collab.tokens`).

Module map
----------
* ``tokens``        - sign/verify a short-lived WS access token (shared secret).
* ``pg_ystore``     - ``BaseYStore`` subclass persisting Y updates to PostgreSQL.
* ``doc_model``     - Y.Doc <-> DB row mapping (sheet arrays, bootstrap, snapshot).
* ``materializer``  - debounced ``Doc.observe`` hook -> ``items_service.materialize_sheet``.
* ``presence``      - cross-process single-writer boundary (lm_collab_presence heartbeat).
* ``server``        - ``WebsocketServer`` + ``ASGIServer(on_connect=auth)`` wiring.

Everything here imports ``pycrdt`` / ``pycrdt-websocket`` lazily so the Flask
web/worker processes (which never touch CRDT) do not need those libraries
installed. Only the collab process must have them.
"""
