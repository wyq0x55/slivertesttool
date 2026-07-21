"""Cross-process collaboration presence — the single-writer boundary signal.

Design doc §1.6 / §12.3. The collab server (``run_collab``) and the Flask web
app run as separate processes, so the web app cannot see the collab server's
in-memory rooms. This module is the tiny shared channel between them, backed by
the ``lm_collab_presence`` table:

* The **collab server** calls :func:`mark_presence` on a heartbeat while a room
  has connected clients, and :func:`clear_presence` when a room is evicted.
* The **web app** calls :func:`is_collab_active` to decide whether a project is
  currently collaborative — i.e. whether the CRDT materializer is the single
  authoritative writer and direct REST row mutations must step aside.

A project is "active" only while its presence row has ``connections > 0`` AND
was refreshed within ``COLLAB_PRESENCE_TTL_SECONDS``. A crashed/stopped collab
server therefore lets the project fall back to classic REST writes once the row
goes stale — the degrade path stays automatic (design doc §1.5).

This module imports ONLY Flask/SQLAlchemy (never ``pycrdt``), so it is safe to
import from the web process. Every DB call is defensive: on any error it logs
and returns a safe default (``False`` for "active" queries, silent no-op for
writes) so presence bookkeeping can never break editing.
"""

from __future__ import annotations

import datetime as _dt
import logging

from ..extensions import db

log = logging.getLogger(__name__)

# Fallbacks used when no Flask config is available (e.g. bare unit contexts).
_DEFAULT_TTL_SECONDS = 30


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ttl_seconds(default: int = _DEFAULT_TTL_SECONDS) -> int:
    try:
        from flask import current_app
        return int(current_app.config.get("COLLAB_PRESENCE_TTL_SECONDS", default))
    except Exception:  # noqa: BLE001 - outside an app context / no config
        return default


def _aware(value: _dt.datetime) -> _dt.datetime:
    """Coerce a possibly naive DB timestamp to an aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


def is_fresh(connections: int, updated_at: _dt.datetime, ttl_seconds: int,
             now: _dt.datetime | None = None) -> bool:
    """Pure predicate: is this presence row 'active'? (DB-free, unit-testable).

    Active == at least one live connection AND refreshed within the TTL. A
    non-positive ``ttl_seconds`` disables the freshness gate (any positive
    connection count counts as active).
    """
    if connections is None or int(connections) <= 0:
        return False
    ref = now or _utcnow()
    age = (ref - _aware(updated_at)).total_seconds()
    if ttl_seconds is not None and int(ttl_seconds) > 0:
        return age <= int(ttl_seconds)
    return True


# --------------------------------------------------------------------------- #
# Writers (collab server side; call inside a Flask app context)
# --------------------------------------------------------------------------- #
def mark_presence(project_id: int, connections: int) -> None:
    """Upsert the heartbeat row for ``project_id``.

    Must run inside a Flask app context. Never raises.
    """
    from ..models import CollabPresence
    try:
        row = db.session.get(CollabPresence, project_id)
        now = _utcnow()
        if row is None:
            db.session.add(CollabPresence(
                project_id=project_id,
                connections=max(0, int(connections)),
                updated_at=now))
        else:
            row.connections = max(0, int(connections))
            row.updated_at = now
        db.session.commit()
    except Exception:  # noqa: BLE001 - presence must never break the server
        log.warning("collab presence upsert failed (project=%s)", project_id,
                    exc_info=True)
        db.session.rollback()


def clear_presence(project_id: int) -> None:
    """Mark a project as having no live collaborators (connections = 0).

    Must run inside a Flask app context. Never raises.
    """
    from ..models import CollabPresence
    try:
        row = db.session.get(CollabPresence, project_id)
        if row is not None:
            row.connections = 0
            row.updated_at = _utcnow()
            db.session.commit()
    except Exception:  # noqa: BLE001
        log.warning("collab presence clear failed (project=%s)", project_id,
                    exc_info=True)
        db.session.rollback()


# --------------------------------------------------------------------------- #
# Readers (web side; call inside a Flask app context / request)
# --------------------------------------------------------------------------- #
def is_collab_active(project_id: int, ttl_seconds: int | None = None) -> bool:
    """Return True iff ``project_id`` currently has live collaborators.

    "Live" == presence row with ``connections > 0`` and ``updated_at`` fresher
    than the TTL. Any error (missing table on an un-migrated DB, etc.) returns
    ``False`` so editing is never blocked by a presence lookup failure.
    """
    from ..models import CollabPresence
    ttl = _ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    try:
        row = db.session.get(CollabPresence, project_id)
        if row is None:
            return False
        return is_fresh(row.connections, row.updated_at, ttl)
    except Exception:  # noqa: BLE001
        log.debug("collab presence lookup failed (project=%s)", project_id,
                  exc_info=True)
        db.session.rollback()
        return False


def active_project_ids(ttl_seconds: int | None = None) -> set[int]:
    """Return the set of project ids that are currently collaborative."""
    from ..models import CollabPresence
    ttl = _ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    try:
        cutoff = _utcnow() - _dt.timedelta(seconds=ttl)
        rows = (CollabPresence.query
                .filter(CollabPresence.connections > 0)
                .filter(CollabPresence.updated_at >= cutoff.replace(tzinfo=None))
                .all())
        return {r.project_id for r in rows}
    except Exception:  # noqa: BLE001
        log.debug("collab active-projects lookup failed", exc_info=True)
        db.session.rollback()
        return set()
