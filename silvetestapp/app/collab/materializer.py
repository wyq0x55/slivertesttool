"""Debounced materialization: project ``Y.Doc`` -> ``TestItemRow`` rows.

One :class:`Materializer` per room. It subscribes to the whole ``Doc`` via
``Doc.observe`` (fires once per transaction — the clean hook confirmed by the
pycrdt introspection) and, after a short debounce, snapshots every sheet array
and reconciles it into the database through
:func:`items_service.materialize_sheet` (uuid-keyed upsert, single transaction).

Because materialization writes to the DB and never back into the ``Y.Doc``, it
cannot create a feedback loop today. (``bootstrap`` runs before this observer is
attached, so it is never seen either.) If a future change writes server data —
e.g. the generated ``id``/``version`` — back into the ``Y.Map``, wrap that write
in :meth:`Materializer.suppressed` so it does not re-trigger a reconcile.

Note on ``TransactionEvent``: in pycrdt 0.14.1 the event has no ``origin``
attribute; the origin lives on ``event.transaction.origin()`` and is returned as
an INTEGER hash, not the original string — so string-based origin matching is
not reliable. We therefore gate self-originated writes with an explicit counter.

The DB work runs in a worker thread inside a Flask app context, so the async
event loop that drives the WebSocket rooms is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Optional

import anyio

from . import awareness as _awareness_mod
from . import doc_model

_log = logging.getLogger("collab.materializer")


class Materializer:
    def __init__(self, project_id: int, flask_app, *,
                 actor_user_id: Optional[int] = None,
                 debounce: float = 3.0) -> None:
        self._pid = project_id
        self._app = flask_app
        self._actor_user_id = actor_user_id
        self._debounce = debounce
        self._doc = None
        # Live Awareness object for this room (best-effort per-row audit
        # attribution); ``None`` keeps the single batch-actor behavior.
        self._awareness = None
        self._sub = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._timer: Optional[asyncio.TimerHandle] = None
        self._flushing = False
        self._dirty_again = False
        # FIX: 用整数计数器替代 bool，嵌套 suppressed() 调用不会提前放开观察器。
        # 进入 +1，退出 -1；只有计数归零才重新响应 Y.Doc 事务。
        self._suppress: int = 0

    def attach(self, doc, awareness=None) -> None:
        """Start observing ``doc``. Call from within the running event loop.

        ``awareness`` (the room's live Awareness object, optional) enables
        per-row audit attribution: on each flush we snapshot it to credit rows
        to the collaborator editing them, falling back to the batch actor.
        """
        self._doc = doc
        self._awareness = awareness
        self._loop = asyncio.get_running_loop()
        self._sub = doc.observe(self._on_txn)

    @contextmanager
    def suppressed(self):
        """将此块内对 Y.Doc 的写入标记为自身发起，观察器将忽略它们。
        使用计数器（非 bool）确保嵌套调用安全：只有所有调用方都退出后
        观察器才重新启用。
        """
        self._suppress += 1
        try:
            yield
        finally:
            self._suppress -= 1

    def detach(self) -> None:
        if self._sub is not None and self._doc is not None:
            try:
                self._doc.unobserve(self._sub)
            except Exception:  # pragma: no cover - best effort on teardown
                pass
        self._sub = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def flush_and_detach(self) -> None:
        """Persist any debounced-but-unwritten changes, then stop observing.

        Called when a room is evicted (idle) or the server shuts down: the CRDT
        state is already durable in the ``YStore``, but a pending debounce timer
        means the DB materialization has not caught up yet. We run one final
        reconcile so no edits are lost, then detach so the ``Y.Doc`` can be torn
        down without leaking this observer. Awaited on the event-loop thread.
        """
        had_pending = (self._timer is not None
                       or self._dirty_again
                       or self._flushing)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if had_pending and self._doc is not None:
            try:
                await self._flush()
            except Exception:  # pragma: no cover - never crash teardown
                _log.exception("final flush failed for project %s", self._pid)
        self.detach()

    # ------------------------------------------------------------------ #
    def _on_txn(self, event: Any) -> None:
        if self._suppress > 0:
            return
        self._schedule()

    def _schedule(self) -> None:
        if self._loop is None:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self._loop.call_later(self._debounce, self._fire)

    def _fire(self) -> None:
        self._timer = None
        if self._flushing:
            # A flush is in flight; remember to run once more when it finishes.
            self._dirty_again = True
            return
        assert self._loop is not None
        self._loop.create_task(self._flush())

    async def _flush(self) -> None:
        self._flushing = True
        try:
            # Snapshot on the loop thread (reads the Y types), then hand the
            # plain dicts to a worker thread for the DB reconcile.
            snapshot = {sheet: doc_model.snapshot_sheet(self._doc, sheet)
                        for sheet in doc_model.sheets()}
            # Best-effort: map row uuid -> editing user id from Awareness so the
            # reconcile can attribute rows to the collaborator who touched them.
            row_uid = _awareness_mod.row_actors(
                _awareness_mod.snapshot_states(self._awareness))
            summary, idmaps = await anyio.to_thread.run_sync(
                self._materialize_sync, snapshot, row_uid)
            _log.info("materialized project %s: %s", self._pid, summary)
            # Push the authoritative id/version back into the Y.Doc on the loop
            # thread; suppressed so it does not re-trigger a reconcile.
            self._apply_id_writeback(idmaps)
        except Exception:  # pragma: no cover - logged, never crashes the loop
            _log.exception("materialization failed for project %s", self._pid)
        finally:
            self._flushing = False
            if self._dirty_again:
                self._dirty_again = False
                self._schedule()

    def _materialize_sync(self, snapshot: dict[str, list[dict]],
                          row_uid: Optional[dict[str, int]] = None):
        """Reconcile every sheet, then read back the authoritative id/version.

        Returns ``(summary, idmaps)`` where ``idmaps`` maps
        ``sheet -> {uuid: (id, version)}`` for the freshly committed rows.

        ``row_uid`` (row uuid -> user id, from Awareness) is resolved to actual
        users once and passed as ``actor_by_uuid`` so each row is credited to
        its editor; rows without an entry keep the batch actor.
        """
        from ..extensions import db
        from ..models import Project
        from ..services.lanmatrix import items_service
        with self._app.app_context():
            project = Project.query.get(self._pid)
            if project is None:
                return {}, {}
            actor = self._resolve_actor(project)
            actor_by_uuid = self._resolve_row_actors(row_uid)
            result: dict[str, dict[str, int]] = {}
            for sheet, rows in snapshot.items():
                result[sheet] = items_service.materialize_sheet(
                    actor, project, sheet, rows, commit=False,
                    actor_by_uuid=actor_by_uuid)
            db.session.commit()
            idmaps = {sheet: items_service.sheet_uuid_index(self._pid, sheet)
                      for sheet in snapshot}
            return result, idmaps

    def _apply_id_writeback(self, idmaps: dict[str, dict]) -> None:
        """Write server id/version onto the Y.Maps (loop thread, suppressed)."""
        if not idmaps or self._doc is None:
            return
        total = 0
        try:
            with self.suppressed():
                with self._doc.transaction(origin="materialize-writeback"):
                    for sheet, id_map in idmaps.items():
                        if id_map:
                            total += doc_model.write_back_ids(
                                self._doc, sheet, id_map)
        except Exception:  # pragma: no cover - never crash the loop
            _log.exception("id write-back failed for project %s", self._pid)
            return
        if total:
            _log.info("wrote back id/version for project %s: %s rows",
                      self._pid, total)

    def _resolve_actor(self, project):
        from ..models import LMUser
        uid = self._actor_user_id or project.owner_id or project.created_by
        actor = LMUser.query.get(uid) if uid else None
        if actor is None:
            # Last resort: any system admin, so audit rows always have an actor.
            actor = LMUser.query.filter_by(is_system_admin=True).first()
        return actor

    def _resolve_row_actors(self, row_uid):
        """Resolve ``{uuid: user_id}`` (from Awareness) to ``{uuid: LMUser}``.

        Loads each referenced user once. Inactive/unknown ids are dropped so the
        row falls back to the batch actor. Runs inside the reconcile's app
        context (called from :meth:`_materialize_sync`).
        """
        if not row_uid:
            return {}
        from ..models import LMUser
        wanted = {int(u) for u in row_uid.values() if u is not None}
        if not wanted:
            return {}
        users = {u.id: u for u in
                 LMUser.query.filter(LMUser.id.in_(wanted)).all()
                 if u.is_active}
        out = {}
        for row_uuid, uid in row_uid.items():
            user = users.get(int(uid)) if uid is not None else None
            if user is not None:
                out[row_uuid] = user
        return out
