"""Safe publication of server-computed field values onto test-matrix rows.

Why this module exists
----------------------
A finished test run has to land on the matching row of the test matrix. That
sounds like a plain ``UPDATE``, and in classic (REST) mode it is. But when the
project is open in the collaborative editor, the ``Y.Doc`` -- not the database
-- is the authoritative copy of every row: :mod:`app.collab.materializer`
reconciles ``Y.Doc -> DB`` in *one direction only*, on a 3s debounce. A direct
``UPDATE`` therefore survives for at most one flush before being silently
overwritten with whatever the editor still shows. With a single ``result``
column that was easy to miss; the run write-back now touches five columns, so
it would be obvious and constant.

The processes are separate, though: the write comes from the Huey worker, while
the ``Y.Doc`` only exists inside the collab (ASGI) process. So the worker leaves
its intent in a small queue table (:class:`~app.models.lanmatrix.RowWriteback`)
and the collab server drains it into the live room, inside the materializer's
``suppressed()`` block -- exactly the extension point the materializer's own
docstring reserves for this.

Ordering guarantee
------------------
The database row is *always* written first and stays authoritative; the queue
only mirrors the same values into the Doc so the editor cannot revert them.
Consequently:

* if the collab server is down, nothing is lost -- the value is in the database
  and a room bootstrapped later reads it from there;
* a queued item that goes stale (``COLLAB_WRITEBACK_TTL_SECONDS``) is dropped
  rather than replayed onto a much newer Doc, where it could undo a manual edit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from flask import current_app

from ..extensions import db
from ..models import RowWriteback, TestItemRow
from . import presence

_log = logging.getLogger(__name__)

# Sheets the write-back path is allowed to touch. Server-computed values only
# ever belong to test rows; guarding it keeps a bad caller from corrupting the
# constant/library sheets.
_ALLOWED_SHEETS = ("test",)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def apply_server_fields(project_id: int, row: TestItemRow,
                        values: Mapping[str, Any], *,
                        sheet: str = "test") -> bool:
    """Write ``values`` (``field_key -> value``) onto ``row``, collab-safely.

    Returns ``True`` when at least one field actually changed. The caller is
    responsible for committing the session: this keeps the row update and
    whatever else the caller is writing (a run record, a notification) inside a
    single transaction.
    """
    if sheet not in _ALLOWED_SHEETS:
        raise ValueError(f"write-back is not allowed on sheet {sheet!r}")
    if not values:
        return False

    changed = False
    for key, value in values.items():
        try:
            if row.get_field(key) != value:
                row.set_field(key, value)
                changed = True
        except Exception:
            # A field that does not exist in this project's schema is a
            # configuration problem, not a reason to lose the whole verdict.
            _log.warning("write-back skipped unknown field %r (project=%s)",
                         key, project_id)
    if not changed:
        return False

    row.version = (row.version or 0) + 1
    row.updated_at = _utcnow()

    # Mirror into the CRDT document only while somebody is actually editing;
    # otherwise the next room bootstrap picks the values up from the database.
    if row.uuid and presence.is_collab_active(project_id):
        db.session.add(RowWriteback(
            project_id=project_id,
            sheet=sheet,
            row_uuid=row.uuid,
            payload={k: v for k, v in values.items()},
        ))
        _log.info("queued collab write-back: project=%s row=%s fields=%s",
                  project_id, row.uuid, sorted(values))
    return True


# --------------------------------------------------------------------------- #
# Collab-server side: draining the queue
# --------------------------------------------------------------------------- #
def claim_pending(project_ids: Iterable[int]) -> dict[int, dict[str, dict]]:
    """Fetch and mark-as-applied the pending write-backs for live rooms.

    Returns ``{project_id: {row_uuid: {field: value}}}``, already merged so a
    row queued twice in the same window is applied once with the newest value
    winning. Rows are marked applied up front: re-applying a write-back is
    harmless (it is idempotent), but replaying an unbounded backlog after a
    transient Doc error is not.

    Runs in the collab process, inside an app context.
    """
    project_ids = [int(p) for p in project_ids]
    if not project_ids:
        return {}

    ttl = int(current_app.config.get("COLLAB_WRITEBACK_TTL_SECONDS", 900) or 0)
    rows = (RowWriteback.query
            .filter(RowWriteback.project_id.in_(project_ids))
            .filter(RowWriteback.applied_at.is_(None))
            .order_by(RowWriteback.id.asc())
            .limit(2000)
            .all())
    if not rows:
        return {}

    now = _utcnow()
    cutoff = now - timedelta(seconds=ttl) if ttl > 0 else None
    out: dict[int, dict[str, dict]] = {}
    stale = 0
    for item in rows:
        item.applied_at = now
        if cutoff is not None and (item.created_at or now) < cutoff:
            stale += 1
            continue
        if not item.row_uuid or not item.payload:
            continue
        bucket = out.setdefault(item.project_id, {})
        bucket.setdefault(item.row_uuid, {}).update(item.payload)
    db.session.commit()
    if stale:
        _log.warning("dropped %s stale collab write-back(s)", stale)
    return out


def purge_applied(older_than_seconds: int = 3600) -> int:
    """Delete write-backs applied longer than ``older_than_seconds`` ago."""
    cutoff = _utcnow() - timedelta(seconds=max(0, older_than_seconds))
    deleted = (RowWriteback.query
               .filter(RowWriteback.applied_at.isnot(None))
               .filter(RowWriteback.applied_at < cutoff)
               .delete(synchronize_session=False))
    db.session.commit()
    return int(deleted or 0)
