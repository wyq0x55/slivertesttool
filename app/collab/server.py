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
import time
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
        # Unbounded growth is avoided by the idle sweeper below (see
        # ``_sweep_loop``): a room with no connected client is evicted after
        # ``COLLAB_ROOM_IDLE_TTL_SECONDS`` and rehydrated from PgYStore on the
        # next connection.
        kw.setdefault("auto_clean_rooms", False)
        super().__init__(**kw)
        self._app = flask_app
        self._rooms: dict[str, YRoom] = {}
        self._materializers: dict[str, Materializer] = {}
        # name -> monotonic timestamp of the last moment the room had a client.
        self._last_active: dict[str, float] = {}
        cfg = flask_app.config
        self._idle_ttl = float(cfg.get("COLLAB_ROOM_IDLE_TTL_SECONDS", 900) or 0)
        self._sweep_interval = float(cfg.get("COLLAB_ROOM_SWEEP_SECONDS", 60) or 60)
        # Presence heartbeat: publish live-room connection counts to
        # lm_collab_presence so the web process knows which projects are
        # collaborative (single-writer boundary; design doc §1.6 / §12.3).
        self._heartbeat_interval = float(
            cfg.get("COLLAB_PRESENCE_HEARTBEAT_SECONDS", 10) or 10)

    async def get_room(self, name: str) -> YRoom:
        room = self._rooms.get(name)
        if room is None:
            room = await self._create_room(name)
            self._rooms[name] = room
        self._last_active[name] = time.monotonic()
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

        # 3) Attach debounced materialization (Y.Doc -> TestItemRow). The room's
        # Awareness (if the pycrdt-websocket build exposes one) lets the
        # materializer credit each row to the collaborator editing it.
        mat = Materializer(pid, self._app)
        mat.attach(ydoc, awareness=getattr(room, "awareness", None))
        self._materializers[name] = mat
        _log.info("collab room ready: %s (hydrated=%s)", name, loaded)
        return room

    def _bootstrap_sync(self, ydoc: Doc, pid: int) -> None:
        with self._app.app_context():
            n = doc_model.bootstrap_doc(ydoc, pid)
        _log.info("bootstrapped project %s from DB: %s rows", pid, n)

    # ------------------------------------------------------------------ #
    # Lifecycle: start the idle sweeper with the server, detach on stop.
    # ------------------------------------------------------------------ #
    async def __aenter__(self):
        await super().__aenter__()
        # Run the idle-eviction sweeper inside the server's task group so it is
        # cancelled automatically when the server stops.
        if self._idle_ttl > 0 and self._task_group is not None:
            self._task_group.start_soon(self._sweep_loop)
            _log.info("collab idle sweeper on: ttl=%ss interval=%ss",
                      self._idle_ttl, self._sweep_interval)
        # Publish presence heartbeats so the web app can enforce the
        # single-writer boundary while rooms are live.
        if self._heartbeat_interval > 0 and self._task_group is not None:
            self._task_group.start_soon(self._heartbeat_loop)
            _log.info("collab presence heartbeat on: interval=%ss",
                      self._heartbeat_interval)
        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        # Detach every Materializer so no Y.Doc observer leaks past shutdown.
        for name, mat in list(self._materializers.items()):
            try:
                mat.detach()
            except Exception:  # pragma: no cover - best effort on teardown
                _log.exception("materializer detach failed on shutdown: %s", name)
        self._materializers.clear()
        # Best-effort: mark every project as having no live collaborators so the
        # web app stops treating them as collaborative the moment we shut down.
        try:
            pids = [int(n.split(":", 1)[1]) for n in self._rooms.keys()]
            if pids:
                await anyio.to_thread.run_sync(self._clear_presence_sync, pids)
        except Exception:  # pragma: no cover - teardown best effort
            _log.exception("collab presence clear-on-shutdown failed")
        self._rooms.clear()
        self._last_active.clear()
        return await super().__aexit__(exc_type, exc_value, exc_tb)

    # ------------------------------------------------------------------ #
    # Presence heartbeat (single-writer boundary signal for the web app)
    # ------------------------------------------------------------------ #
    async def _heartbeat_loop(self) -> None:
        while True:
            await anyio.sleep(self._heartbeat_interval)
            try:
                counts = {
                    int(name.split(":", 1)[1]): len(getattr(room, "clients", ()) or ())
                    for name, room in self._rooms.items()
                }
                if counts:
                    await anyio.to_thread.run_sync(self._heartbeat_sync, counts)
            except Exception:  # pragma: no cover - heartbeat must never die
                _log.exception("collab presence heartbeat iteration failed")

    def _heartbeat_sync(self, counts: dict) -> None:
        from . import presence
        with self._app.app_context():
            for pid, n in counts.items():
                presence.mark_presence(pid, n)

    def _clear_presence_sync(self, pids: list) -> None:
        from . import presence
        with self._app.app_context():
            for pid in pids:
                presence.clear_presence(pid)

    async def _sweep_loop(self) -> None:
        """Periodically evict rooms that have been client-less past the TTL."""
        while True:
            await anyio.sleep(self._sweep_interval)
            try:
                await self._evict_idle_rooms()
            except Exception:  # pragma: no cover - sweeper must never die
                _log.exception("collab idle sweep iteration failed")

    async def _evict_idle_rooms(self) -> None:
        now = time.monotonic()
        for name in list(self._rooms.keys()):
            room = self._rooms.get(name)
            if room is None:
                continue
            if room.clients:
                # Still in use: reset the idle clock.
                self._last_active[name] = now
                continue
            last = self._last_active.get(name, now)
            if now - last >= self._idle_ttl:
                await self._evict_room(name)

    async def _evict_room(self, name: str) -> None:
        """Tear down one idle room: final materialize, detach, stop, forget.

        The durable CRDT state lives in :class:`PgYStore`, so a re-connection
        after eviction rebuilds the room via :meth:`_create_room`.
        """
        room = self._rooms.pop(name, None)
        # A client may have connected in the tiny window before this coroutine
        # ran; if so, keep the room and refresh its idle clock.
        if room is not None and room.clients:
            self._rooms[name] = room
            self._last_active[name] = time.monotonic()
            return
        mat = self._materializers.pop(name, None)
        self._last_active.pop(name, None)
        if mat is not None:
            try:
                await mat.flush_and_detach()
            except Exception:  # pragma: no cover
                _log.exception("materializer flush/detach failed: %s", name)
        if room is not None:
            try:
                await room.stop()
            except Exception:  # pragma: no cover
                _log.exception("room stop failed: %s", name)
        # Room is gone: tell the web app this project is no longer collaborative
        # so REST writes resume immediately (don't wait for the presence TTL).
        try:
            pid = int(name.split(":", 1)[1])
            await anyio.to_thread.run_sync(self._clear_presence_sync, [pid])
        except Exception:  # pragma: no cover
            _log.exception("collab presence clear-on-evict failed: %s", name)
        _log.info("evicted idle collab room: %s", name)


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
