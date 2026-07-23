"""Cross-process, runtime-adjustable license gate.

Silver is licensed for a fixed number of concurrent instances. Because the web
process and the Huey worker run separately, the classic in-memory semaphore no
longer works -- the gate lives in the shared PostgreSQL database instead.

Two ``app_settings`` rows implement it:

* ``license_limit`` -- the maximum number of concurrent Silver runs. Editable at
  runtime from the admin page; changes apply live (queued tasks pick up freed
  slots without a worker restart).
* ``license_inuse`` -- the number of slots currently held.

Acquisition is a single conditional ``UPDATE`` executed atomically by
PostgreSQL (the row-level write lock serialises concurrent updaters), so it is
atomic across processes with no lost updates.
"""

from __future__ import annotations

from sqlalchemy import text

from ..extensions import db
from ..models import Setting


def _get_int(key: str, default: int = 0) -> int:
    row = db.session.get(Setting, key)
    if row is None:
        return default
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return default


def _set_int(key: str, value: int) -> None:
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=str(int(value)))
        db.session.add(row)
    else:
        row.value = str(int(value))


def init_defaults(limit: int) -> None:
    """Seed the license rows if they do not exist. Idempotent.

    Also self-heals a limit of ``0`` left behind by :func:`begin_drain` (the
    graceful-shutdown drain): on the next startup it is restored to the configured
    default so Silver is usable again. Any positive operator-set value is kept.
    """
    limit_row = db.session.get(Setting, Setting.LICENSE_LIMIT)
    if limit_row is None:
        db.session.add(Setting(key=Setting.LICENSE_LIMIT, value=str(int(limit))))
    elif _get_int(Setting.LICENSE_LIMIT, limit) < 1:
        # Restore a drained (0) limit back to the configured default.
        limit_row.value = str(int(limit))
    if db.session.get(Setting, Setting.LICENSE_INUSE) is None:
        db.session.add(Setting(key=Setting.LICENSE_INUSE, value="0"))
    db.session.commit()


def get_limit() -> int:
    return _get_int(Setting.LICENSE_LIMIT, 1)


def get_in_use() -> int:
    return _get_int(Setting.LICENSE_INUSE, 0)


def get_status() -> dict:
    limit = get_limit()
    in_use = get_in_use()
    return {
        "total": limit,
        "in_use": in_use,
        "available": max(0, limit - in_use),
    }


def set_limit(new_limit: int) -> int:
    """Update the concurrency limit (>= 1). Returns the applied value."""
    if new_limit < 1:
        raise ValueError("license limit must be >= 1")
    _set_int(Setting.LICENSE_LIMIT, new_limit)
    db.session.commit()
    return new_limit


def begin_drain() -> None:
    """Force the concurrency limit to 0 for a graceful shutdown.

    Unlike :func:`set_limit` (which enforces ``>= 1`` for normal operation),
    this writes ``license_limit = 0`` directly. The worker's reconcile loop then
    shrinks the Silver pool target to 0 on its next tick, disposing pooled Silver
    instances cleanly (and releasing their licenses) instead of relying on a hard
    force-kill. Intended to be called only from the launcher's shutdown path.
    """
    _set_int(Setting.LICENSE_LIMIT, 0)
    db.session.commit()


def try_acquire() -> bool:
    """Atomically take one slot if one is free. Returns True on success."""
    stmt = text(
        "UPDATE app_settings "
        "SET value = CAST(value AS INTEGER) + 1 "
        "WHERE key = :inuse "
        "AND CAST(value AS INTEGER) < "
        "(SELECT CAST(value AS INTEGER) FROM app_settings WHERE key = :limit)"
    )
    result = db.session.execute(
        stmt, {"inuse": Setting.LICENSE_INUSE, "limit": Setting.LICENSE_LIMIT}
    )
    db.session.commit()
    return result.rowcount == 1


def release() -> None:
    """Return one slot to the pool (never drops below zero)."""
    stmt = text(
        "UPDATE app_settings "
        "SET value = GREATEST(CAST(value AS INTEGER) - 1, 0) "
        "WHERE key = :inuse"
    )
    db.session.execute(stmt, {"inuse": Setting.LICENSE_INUSE})
    db.session.commit()


def reset_in_use() -> None:
    """Reset the in-use counter to zero (used on worker startup recovery)."""
    _set_int(Setting.LICENSE_INUSE, 0)
    db.session.commit()


def mark_busy() -> None:
    """Record that one pre-warmed pool instance started running a test.

    Used by the pooled execution path where the concurrency limit is enforced
    by the pool itself, so this is a plain (limit-bounded) increment kept only
    for the admin dashboard's in-use display. Cross-process safe via
    PostgreSQL's row-level write lock.
    """
    stmt = text(
        "UPDATE app_settings "
        "SET value = LEAST(CAST(value AS INTEGER) + 1, "
        "(SELECT CAST(value AS INTEGER) FROM app_settings WHERE key = :limit)) "
        "WHERE key = :inuse"
    )
    db.session.execute(
        stmt, {"inuse": Setting.LICENSE_INUSE, "limit": Setting.LICENSE_LIMIT}
    )
    db.session.commit()


def mark_idle() -> None:
    """Record that a pool instance finished a test (bounded decrement)."""
    release()
