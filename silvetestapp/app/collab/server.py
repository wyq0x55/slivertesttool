"""Assemble the collaboration ASGI app: rooms, persistence, auth, materialize.

Wiring (all classes confirmed by the pycrdt / pycrdt-websocket introspection):

* :class:`CollabWebsocketServer` overrides :meth:`get_room` to build one
  ``YRoom`` per project with a project ``Doc``, a :class:`PgYStore`, initial
  hydration (replay persisted updates, else bootstrap from the DB), and an
  attached :class:`Materializer`.
* :func:`build_asgi_app` wraps it in ``ASGIServer`` with an ``on_connect``
  callback that verifies the signed access token minted by the Flask app.

Contracts VERIFIED against the installed pycrdt-websocket 0.16.4 source:
  1. ``ASGIServer`` invokes ``on_connect(message, scope)`` (the connect message
     first, the ASGI scope second) and treats a *truthy* return as "close the
     socket" (reject); a falsy return accepts it.
  2. ``WebsocketServer.serve(ws)`` dispatches via ``get_room(ws.path)`` where
     ``ws.path == scope['path']`` INCLUDING the leading '/', then calls
     ``start_room(room)`` (idempotent).
  3. ``serve``/``start_room`` require the server's task group to be running, so
     the ASGI app must start the ``WebsocketServer`` during ``lifespan.startup``
     (``ASGIServer`` itself does NOT start it) — done in :func:`build_asgi_app`.
  4. ``BaseYStore.start`` is a usable default (no DB-init needed); ``PgYStore``
     inherits it. Verified by a live run.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

import anyio
from pycrdt import Doc
from pycrdt.store import YDocNotFound
from pycrdt.websocket import ASGIServer, WebsocketServer, YRoom

from . import doc_model, tokens
from .materializer import Materializer
from .pg_ystore import PgYStore

_log = logging.getLogger("collab.server")


def _room_from_scope(scope: dict) -> str:
    return (scope.get("path") or "").lstrip("/")


def _token_from_scope(scope: dict) -> str:
    qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    vals = qs.get("token") or qs.get("access_token") or []
    return vals[0] if vals else ""


class CollabWebsocketServer(WebsocketServer):
    """A WebsocketServer whose rooms are PG-backed project documents."""

    def __init__(self, flask_app, **kw) -> None:
        # Keep rooms alive so their Materializer keeps flushing while at least
        # one project session may reconnect; PgYStore holds the durable state.
        kw.setdefault("auto_clean_rooms", False)
        super().__init__(**kw)
        self._app = flask_app
        self._rooms: dict[str, YRoom] = {}
        self._materializers: dict[str, Materializer] = {}

    async def get_room(self, name: str) -> YRoom:
        room = self._rooms.get(name)
        if room is None:
            room = await self._create_room(name)
            self._rooms[name] = room
        return room

    async def _create_room(self, name: str) -> YRoom:
        pid = int(name.split(":", 1)[1])
        ydoc = Doc()
        ystore = PgYStore(name, self._app)

        # 1) Hydrate: replay persisted CRDT updates; if none, seed from the DB.
        loaded = False
        try:
            async for update, _meta, _ts in ystore.read():
                ydoc.apply_update(update)
                loaded = True
        except YDocNotFound:
            loaded = False
        if not loaded:
            await anyio.to_thread.run_sync(self._bootstrap_sync, ydoc, pid)
            # Persist the seed so we never bootstrap this room twice.
            await ystore.write(ydoc.get_update())
        else:
            # Existing doc: make sure every sheet array key exists.
            doc_model.ensure_sheets(ydoc)

        # 2) Build the room (ystore persists every future update) and start it.
        room = YRoom(ydoc=ydoc, ystore=ystore)
        await self.start_room(room)

        # 3) Attach debounced materialization (Y.Doc -> TestItemRow).
        mat = Materializer(pid, self._app)
        mat.attach(ydoc)
        self._materializers[name] = mat
        _log.info("collab room ready: %s (hydrated=%s)", name, loaded)
        return room

    def _bootstrap_sync(self, ydoc: Doc, pid: int) -> None:
        with self._app.app_context():
            n = doc_model.bootstrap_doc(ydoc, pid)
        _log.info("bootstrapped project %s from DB: %s rows", pid, n)


def build_asgi_app(flask_app):
    """Return an ASGI application serving authenticated project rooms.

    ``ASGIServer`` handles the WebSocket protocol but does NOT start the
    ``WebsocketServer`` task group, so we wrap it: the ``lifespan.startup``
    event enters the server (``__aenter__``), and ``lifespan.shutdown`` exits it.
    """
    secret_key = flask_app.config["SECRET_KEY"]
    ws_server = CollabWebsocketServer(flask_app)

    async def on_connect(message: dict, scope: dict):
        # Return a TRUTHY value to close/reject the socket, falsy to accept it.
        room = _room_from_scope(scope)
        payload = tokens.verify(secret_key, _token_from_scope(scope))
        if not payload:
            _log.warning("collab connect rejected: bad/expired token (room=%s)", room)
            return True
        if payload.get("room") != room:
            _log.warning("collab connect rejected: room mismatch %s != %s",
                         payload.get("room"), room)
            return True
        return False  # accept

    inner = ASGIServer(ws_server, on_connect=on_connect)

    async def _http_reply(send, status: int, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def app(scope: dict, receive, send):
        if scope["type"] == "http":
            # This ASGI app speaks WebSocket only. A plain HTTP request reaching
            # here almost always means uvicorn accepted the connection but had NO
            # WebSocket library, so it downgraded the client's `Upgrade` request
            # to ordinary HTTP. Reply with a clear message instead of crashing
            # with "ASGI callable returned without starting response" (500).
            path = scope.get("path") or "/"
            if path == "/" or path == "/healthz":
                await _http_reply(send, 200, b"collab server up (websocket only)\n")
            else:
                await _http_reply(
                    send, 426,
                    b"This endpoint requires a WebSocket upgrade. If you reached "
                    b"it over plain HTTP, the collab server is missing a WebSocket "
                    b"library: install it with `pip install websockets` (or "
                    b"`pip install -r requirements-collab.txt`) and restart.\n",
                )
            return
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    try:
                        await ws_server.__aenter__()
                    except Exception as exc:  # pragma: no cover
                        _log.exception("collab server startup failed")
                        await send({"type": "lifespan.startup.failed", "message": str(exc)})
                        return
                    _log.info("collab websocket server started")
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    try:
                        await ws_server.__aexit__(None, None, None)
                    except Exception:  # pragma: no cover
                        _log.exception("collab server shutdown error")
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await inner(scope, receive, send)

    return app
