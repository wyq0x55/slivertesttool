"""PostgreSQL session lock that permits one live worker runtime."""

from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from typing import Callable

from sqlalchemy import text

logger = logging.getLogger("silvetestapp.worker")

_LOCK_NAME = "silver-test-platform:worker-runtime"
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(_LOCK_NAME.encode("utf-8")).digest()[:8], "big", signed=True
)
_MUTATION_KEY = int.from_bytes(
    hashlib.sha256(f"{_LOCK_NAME}:mutation".encode("utf-8")).digest()[:8],
    "big", signed=True,
)
_ownership_lost = threading.Event()
_active_owner = None


class WorkerAlreadyRunning(RuntimeError):
    """Another live worker holds the runtime lock."""


def ownership_was_lost() -> bool:
    """True after this process sees its runtime-lock session disappear."""
    return _ownership_lost.is_set()


def ownership_is_current() -> bool:
    """Synchronously check the original session without reconnecting it."""
    return not ownership_was_lost() and (_active_owner is None or _active_owner._ping())


class WorkerLock:
    """A session advisory lock held by one dedicated database connection."""

    def __init__(self, connection) -> None:
        self._connection = connection
        self._released = False
        self._lost = False
        self._db_lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    def supervise(self, on_lost: Callable[[], None], interval: float = 5.0) -> None:
        """Fail closed if this session dies. Never replace the connection."""
        if self._monitor is not None:
            return

        def watch() -> None:
            while not self._released:
                if not self._ping():
                    if self._released:
                        return
                    _ownership_lost.set()
                    try:
                        on_lost()
                    except Exception:
                        logger.exception("Worker lock loss handler failed")
                    return
                if self._stop.wait(interval):
                    return

        self._monitor = threading.Thread(
            target=watch, name="worker-lock", daemon=True
        )
        self._monitor.start()

    def _ping(self) -> bool:
        with self._db_lock:
            if self._released or self._lost:
                return False
            try:
                self._connection.execute(text("SELECT 1"))
                return True
            except Exception:
                self._lost = True
                _ownership_lost.set()
                logger.critical(
                    "Worker runtime lock connection failed; ownership is lost."
                )
                return False

    def release(self) -> None:
        """Unlock and return the connection. Repeated calls are harmless."""
        with self._db_lock:
            if self._released:
                return
            self._released = True
            try:
                if not self._lost:
                    self._connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY}
                    )
            except Exception:
                self._lost = True
                logger.warning(
                    "Could not unlock the worker runtime lock; the session is gone.",
                    exc_info=True,
                )
            finally:
                try:
                    self._connection.close()
                except Exception:
                    logger.warning(
                        "Could not close the worker runtime lock connection.",
                        exc_info=True,
                    )
        self._stop.set()


def acquire() -> WorkerLock:
    """Acquire the worker runtime lock, or fail before the caller mutates state.

    The connection is autocommit so the lock does not leave an idle transaction
    open for the worker's lifetime. Closing or losing the connection releases
    the lock; this process must not open a replacement.
    """
    global _active_owner

    from ..extensions import db

    if db.engine.dialect.name != "postgresql":
        raise RuntimeError("The worker runtime lock requires PostgreSQL.")
    connection = db.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": _MUTATION_KEY}
        )
        try:
            held = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
            ).scalar()
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MUTATION_KEY}
            )
    except Exception:
        connection.close()
        raise
    if not held:
        connection.close()
        raise WorkerAlreadyRunning(
            "Another worker already holds the runtime lock."
        )
    owner = WorkerLock(connection)
    _active_owner = owner
    return owner


@contextmanager
def ownership_guard():
    """Serialize worker mutations with takeover, including filesystem sealing.

    The fence belongs to the same physical connection as the business write.
    A failed connection cannot commit stale writes after another worker takes
    over. Each transaction must enter its own guard before mutating state.
    """
    from ..extensions import db

    if ownership_was_lost():
        yield False
        return
    owner = _active_owner
    if owner is None:
        yield True
        return
    try:
        db.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MUTATION_KEY}
        )
        owned = not ownership_was_lost() and owner._ping()
        if not owned:
            db.session.rollback()
        yield owned
    except BaseException:
        db.session.rollback()
        raise
