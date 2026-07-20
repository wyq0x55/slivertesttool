"""A ``BaseYStore`` that persists Y updates to PostgreSQL (``lm_collab_doc``).

Design doc §7.2. One store instance == one project's document (``path`` ==
``"project:{id}"``). We only implement the two abstract I/O methods —
:meth:`read` (async-iterate the stored updates) and :meth:`write` (append one
update) — and let :class:`pycrdt.store.BaseYStore` drive the start/stop
lifecycle (mirroring how ``SQLiteYStore`` only overrides what it must).

The database work is synchronous SQLAlchemy, so it is dispatched to a worker
thread via ``anyio.to_thread.run_sync`` and wrapped in a Flask app context to
reuse the existing models/session — the collab event loop never blocks on I/O.

NOTE (verify once against the installed 0.16.4): this subclass does NOT override
``start``/``stop``. If ``BaseYStore.start`` turns out to be abstract in your
build (rather than a usable default that only ``SQLiteYStore`` extends for DB
init), add a ``start`` that mirrors ``SQLiteYStore.start`` minus the DB-init
call. A 3-line check:
    import inspect, pycrdt.store as s
    print(inspect.getsource(s.BaseYStore.start))
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Awaitable, Callable

import anyio
from pycrdt import Doc, merge_updates
from pycrdt.store import BaseYStore, YDocNotFound

# Merge the append-only log into a single squashed update once it grows past
# this many rows, to keep room load (replay) cheap.
COMPACT_THRESHOLD = 200


def _pid_from_path(path: str) -> int:
    return int(path.split(":", 1)[1])


class PgYStore(BaseYStore):
    """Store Y updates for one project in ``lm_collab_doc``."""

    def __init__(self, path: str, flask_app,
                 metadata_callback: Callable[[], Awaitable[bytes] | bytes] | None = None,
                 log=None) -> None:
        # Mirror SQLiteYStore: set our own attributes, don't call super().
        self.path = path
        self._app = flask_app
        self._pid = _pid_from_path(path)
        self.metadata_callback = metadata_callback
        self.log = log
        self.lock = anyio.Lock()

    # ------------------------------------------------------------------ #
    # Abstract I/O required by BaseYStore
    # ------------------------------------------------------------------ #
    async def read(self) -> AsyncIterator[tuple[bytes, bytes | None, float]]:
        rows = await anyio.to_thread.run_sync(self._read_sync)
        if not rows:
            raise YDocNotFound
        for update, metadata, ts in rows:
            yield update, metadata, ts

    async def write(self, data: bytes) -> None:
        metadata: bytes | None = None
        if self.metadata_callback is not None:
            md = self.metadata_callback()
            metadata = await md if _is_awaitable(md) else md
        async with self.lock:
            await anyio.to_thread.run_sync(self._write_sync, data, metadata, time.time())

    # ------------------------------------------------------------------ #
    # Synchronous DB helpers (run in a worker thread + Flask app context)
    # ------------------------------------------------------------------ #
    def _read_sync(self) -> list[tuple[bytes, bytes | None, float]]:
        from ..extensions import db
        from ..models import CollabDoc
        with self._app.app_context():
            rows = (CollabDoc.query
                    .filter_by(project_id=self._pid)
                    .order_by(CollabDoc.seq.asc()).all())
            return [(bytes(r.update), (bytes(r.doc_metadata) if r.doc_metadata else None), r.ts)
                    for r in rows]

    def _write_sync(self, data: bytes, metadata: bytes | None, ts: float) -> None:
        from ..extensions import db
        from ..models import CollabDoc
        with self._app.app_context():
            next_seq = (db.session.query(db.func.max(CollabDoc.seq))
                        .filter_by(project_id=self._pid).scalar() or 0) + 1
            db.session.add(CollabDoc(
                project_id=self._pid, seq=next_seq,
                update=data, doc_metadata=metadata, ts=ts))
            db.session.commit()
            if next_seq >= COMPACT_THRESHOLD:
                self._compact_locked()

    def _compact_locked(self) -> None:
        """Squash the whole log into one update at ``seq = 1``. Caller holds the
        async lock and an app context."""
        from ..extensions import db
        from ..models import CollabDoc
        rows = (CollabDoc.query.filter_by(project_id=self._pid)
                .order_by(CollabDoc.seq.asc()).all())
        if len(rows) < 2:
            return
        merged = merge_updates(*[bytes(r.update) for r in rows])
        latest_ts = rows[-1].ts
        for r in rows:
            db.session.delete(r)
        db.session.flush()
        db.session.add(CollabDoc(project_id=self._pid, seq=1,
                                 update=merged, doc_metadata=None, ts=latest_ts))
        db.session.commit()


def _is_awaitable(obj: Any) -> bool:
    return hasattr(obj, "__await__")
