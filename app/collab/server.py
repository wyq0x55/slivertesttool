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

import asyncio
import logging
import time
from urllib.parse import parse_qs

import anyio
from pycrdt import Doc, Map
from pycrdt.store import YDocNotFound
from pycrdt.websocket import ASGIServer, WebsocketServer, YRoom

from . import doc_model, tokens
from .materializer import Materializer
from .pg_ystore import PgYStore

_log = logging.getLogger("collab.server")

# One monotonic receipt per project. All producers serialize on the Project
# lock before allocating queue IDs, so later committed entries have larger IDs.
# The receipt is persisted atomically with fields in a full Doc update.
_WRITEBACK_RECEIPTS_KEY = "server_writeback_receipts"


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
        # Serialize creation/eviction so concurrent reconnects share one Doc and
        # one PgYStore sequence allocator. Live delivery uses the per-room lock.
        self._room_lifecycle_lock = asyncio.Lock()
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
        # Polling controls how quickly committed field writes reach the Doc.
        # Correctness comes from pending-field protection and durable receipts,
        # rather than this interval being shorter than the materializer debounce.
        self._writeback_interval = float(
            cfg.get("COLLAB_WRITEBACK_POLL_SECONDS", 2.0) or 2.0)

    async def get_room(self, name: str) -> YRoom:
        async with self._room_lifecycle_lock:
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

        # 2) Drain the complete offline backlog before observing or exposing the
        # room. Hydration and live delivery use the same persistence/ack path.
        room = YRoom(ydoc=ydoc, ystore=ystore)
        mat = Materializer(pid, self._app)
        delivered = False
        while await self._deliver_writebacks(room, pid, mat):
            delivered = True
        await self.start_room(room)

        # 3) Attach debounced materialization (Y.Doc -> TestItemRow). The room's
        # Awareness (if the pycrdt-websocket build exposes one) lets the
        # materializer credit each row to the collaborator editing it.
        mat.attach(ydoc, awareness=getattr(room, "awareness", None))
        self._materializers[name] = mat
        if delivered:
            mat.schedule_flush()
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
        # Drain server-computed row write-backs (finished-run verdicts) into the
        # live Y.Docs; without this they would be reverted by the materializer.
        if self._writeback_interval > 0 and self._task_group is not None:
            self._task_group.start_soon(self._writeback_loop)
            _log.info("collab write-back drain on: interval=%ss",
                      self._writeback_interval)
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

    # ------------------------------------------------------------------ #
    # Server -> Doc write-back drain
    # ------------------------------------------------------------------ #
    async def _writeback_loop(self) -> None:
        """Apply queued server-computed field values onto the live Y.Docs.

        See :mod:`app.collab.writeback` for why this indirection exists: the
        worker that finalises a run cannot reach the Y.Doc, and a plain database
        write would be overwritten by the next materializer flush.
        """
        while True:
            await anyio.sleep(self._writeback_interval)
            try:
                await self._drain_writebacks()
            except Exception:  # pragma: no cover - drain must never die
                _log.exception("collab write-back drain iteration failed")

    async def _drain_writebacks(self) -> None:
        # Give every room a batch per round so a large backlog in one project
        # does not starve the others. Repeat until all current backlogs drain.
        names = list(self._rooms)
        failure = None
        while names:
            more = []
            for name in names:
                room = self._rooms.get(name)
                mat = self._materializers.get(name)
                if room is None or mat is None:
                    continue
                try:
                    if await self._deliver_writebacks(
                            room, int(name.split(":", 1)[1]), mat,
                            live_name=name):
                        more.append(name)
                except Exception as exc:
                    # Keep its batch pending and let other rooms make progress.
                    _log.exception("collab write-back delivery failed: %s", name)
                    if failure is None:
                        failure = exc
            names = more
        if failure is not None:
            raise failure

    async def _deliver_writebacks(self, room: YRoom, pid: int,
                                 mat: Materializer, *,
                                 live_name: str | None = None) -> bool:
        """Deliver one ordered batch, with a durable receipt before exact ack.

        A failed store write leaves the in-memory receipt in place. The retry
        persists the full Doc again, even if nothing changed, before acking.
        A failed ack or process restart reuses that receipt and never replays
        the old values over browser edits made since their first delivery.
        """
        from . import writeback
        async with mat.write_lock:
            # Eviction may have won the lock after the drain chose this room.
            if live_name is not None and (
                    self._rooms.get(live_name) is not room
                    or self._materializers.get(live_name) is not mat):
                return False
            entries = await anyio.to_thread.run_sync(
                self._pending_writebacks_sync, [pid])
            if not entries:
                return False
            doc = room.ydoc
            receipts = doc.get(_WRITEBACK_RECEIPTS_KEY, type=Map)
            watermark = int(receipts.get(str(pid), "0"))
            fresh = [entry for entry in entries if entry["id"] > watermark]
            by_sheet = {}
            for entry in fresh:
                sheet = entry["sheet"]
                if sheet not in doc_model.sheets():
                    raise ValueError(f"invalid write-back sheet {sheet!r}")
                by_sheet.setdefault(sheet, {}).setdefault(
                    entry["row_uuid"], {}).update(
                        writeback.expand_aliases(entry["payload"]))
            targets = (await anyio.to_thread.run_sync(
                self._writeback_targets_sync, pid,
                {sheet: set(by_uuid) for sheet, by_uuid in by_sheet.items()}
            )) if by_sheet else {}
            with mat.suppressed():
                with doc.transaction(origin="server-writeback"):
                    for sheet, by_uuid in by_sheet.items():
                        states, order, deleted = targets[sheet]
                        doc_model.remove_rows(doc, sheet, deleted)
                        # Only hydration repairs an incomplete historical Doc.
                        # Absence in a live room is a browser deletion, which
                        # wins over an undelivered field patch.
                        if live_name is None:
                            doc_model.restore_rows(doc, sheet, states, order)
                        doc_model.write_row_fields(doc, sheet, by_uuid)
                    if fresh:
                        receipts[str(pid)] = str(fresh[-1]["id"])
            # This explicit full update is required even on receipt-only retries
            # and no-op values. YRoom's asynchronous observer is not an ack.
            await room.ystore.write(doc.get_update())
            await anyio.to_thread.run_sync(
                self._acknowledge_writebacks_sync,
                [entry["id"] for entry in entries])
            mat.schedule_flush()
            return True

    def _pending_writebacks_sync(self, pids: list) -> list[dict]:
        from . import writeback
        with self._app.app_context():
            return writeback.pending_batch(pids)

    def _acknowledge_writebacks_sync(self, ids: list[int]) -> None:
        from . import writeback
        with self._app.app_context():
            writeback.acknowledge(ids)

    def _writeback_targets_sync(self, pid: int, targets: dict) -> dict:
        """Load live outbox targets and identify later deletions.

        A deleted target may still exist in persisted CRDT history. Remove it
        before acknowledging its older patch, or materialization resurrects it.
        """
        from ..models import TestItemRow
        with self._app.app_context():
            result = {}
            for sheet, uuids in targets.items():
                # One snapshot supplies both targets and insertion order, so
                # a concurrent deletion cannot leave a target without a rank.
                rows = TestItemRow.query.filter_by(
                    project_id=pid, sheet=sheet, deleted_at=None).all()
                states = [row.to_dict() for row in rows if row.uuid in uuids]
                order = {row.uuid: (row.row_order, row.id) for row in rows}
                deleted = uuids - set(order)
                result[sheet] = states, order, deleted
            return result

    async def _sweep_loop(self) -> None:
        """Periodically evict rooms that have been client-less past the TTL."""
        while True:
            await anyio.sleep(self._sweep_interval)
            try:
                await self._evict_idle_rooms()
            except Exception:  # pragma: no cover - sweeper must never die
                _log.exception("collab idle sweep iteration failed")
            try:
                # ``lm_row_writebacks`` is append-only otherwise: nothing else
                # in the project calls ``purge_applied``.
                await anyio.to_thread.run_sync(self._purge_writebacks_sync)
            except Exception:  # pragma: no cover - sweeper must never die
                _log.exception("collab write-back purge failed")

    def _purge_writebacks_sync(self) -> None:
        from . import writeback
        with self._app.app_context():
            deleted = writeback.purge_applied()
            if deleted:
                _log.info("purged %s applied collab write-back row(s)", deleted)

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
        async with self._room_lifecycle_lock:
            room = self._rooms.get(name)
            if room is None:
                return
            # Recheck after waiting for creation/another eviction.
            if room.clients:
                self._last_active[name] = time.monotonic()
                return
            mat = self._materializers.get(name)
            if mat is not None:
                async with mat.write_lock:
                    if room.clients:
                        self._last_active[name] = time.monotonic()
                        return
                    await mat._flush_and_detach_locked()
                    await self._stop_and_forget_room(name, room)
            else:
                await self._stop_and_forget_room(name, room)

    async def _stop_and_forget_room(self, name: str, room: YRoom) -> None:
        """Stop a room while its lifecycle and materializer locks are held."""
        try:
            await room.stop()
        except Exception:  # pragma: no cover
            _log.exception("room stop failed: %s", name)
        self._rooms.pop(name, None)
        self._materializers.pop(name, None)
        self._last_active.pop(name, None)
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
