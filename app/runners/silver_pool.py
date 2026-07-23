"""A pre-warmed pool of reusable Silver instances.

Motivation
----------
Launching a Silver process is slow. The classic flow launched a *fresh* Silver
instance for every queued test and disposed it afterwards, so every run paid the
full start-up cost. This module keeps a fixed number of Silver instances alive
and reuses them:

* At start-up the worker pre-warms ``license_count`` instances (see
  :meth:`SilverInstancePool.prewarm`). Each open instance holds one Silver
  license, so the licenses are *pre-empted* the moment the platform starts.
* Per test the pool lends an idle instance
  (:meth:`SilverInstancePool.acquire`); the runner merely re-opens the model
  and reconfigures the modules (fast) instead of spawning a new process.
* Afterwards the instance is returned to the pool
  (:meth:`SilverInstancePool.release`) instead of being exited, ready for the
  next test.

The pool doubles as the concurrency gate: at most ``target`` instances exist, so
at most ``target`` tests run at once -- exactly the licensed limit.

Design notes
------------
* The pool lives in the **worker process** (Silver runs there) and is shared by
  all worker threads, so every operation is guarded by a single
  :class:`threading.Condition`.
* Creating a real Silver instance is slow, so instance creation happens
  *outside* the lock using a reservation counter (``_size``); other threads keep
  making progress meanwhile.
* This module deliberately imports **no Flask/database** symbols so its logic can
  be unit-tested in isolation with the mock driver.
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from .silver_runner import (
    MockSilverRunner,
    RunContext,
    SilverRunner,
)

logger = logging.getLogger("silver.pool")


# --------------------------------------------------------------------------- #
# Drivers -- backend-specific lifecycle for one instance
# --------------------------------------------------------------------------- #
class InstanceDriver:
    """Backend-specific operations the pool needs for one instance."""

    is_mock: bool = False

    def create(self, sil_path: Path, console_log: Path) -> Any:  # pragma: no cover
        raise NotImplementedError

    def configure_and_run(self, handle: Any, ctx: RunContext) -> None:  # pragma: no cover
        raise NotImplementedError

    def dispose(self, handle: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def force_stop(self, handle: Any) -> None:  # pragma: no cover
        raise NotImplementedError


class RealInstanceDriver(InstanceDriver):
    """Drive a real Synopsys Silver instance via :class:`SilverRunner`."""

    is_mock = False

    def __init__(self, gui: bool = False) -> None:
        self._runner = SilverRunner()
        self._gui = gui

    def create(self, sil_path: Path, console_log: Path) -> Any:
        return self._runner.launch(Path(sil_path), self._gui, Path(console_log))

    def configure_and_run(self, handle: Any, ctx: RunContext) -> None:
        self._runner.configure_and_run(handle, ctx)

    def dispose(self, handle: Any) -> None:
        SilverRunner._safe_dispose(handle)

    def force_stop(self, handle: Any) -> None:
        SilverRunner.force_stop(handle)


class MockInstanceDriver(InstanceDriver):
    """Drive a simulated instance via :class:`MockSilverRunner` (no license)."""

    is_mock = True

    def __init__(self, delay_seconds: float = 0.5) -> None:
        self._runner = MockSilverRunner(delay_seconds)

    def create(self, sil_path: Path, console_log: Path) -> Any:
        return self._runner.launch(Path(sil_path), False, Path(console_log))

    def configure_and_run(self, handle: Any, ctx: RunContext) -> None:
        self._runner.configure_and_run(handle, ctx)

    def dispose(self, handle: Any) -> None:
        exit_fn = getattr(handle, "exit", None)
        if callable(exit_fn):
            try:
                exit_fn(force=True)
            except Exception:  # noqa: BLE001 - best effort
                pass

    def force_stop(self, handle: Any) -> None:
        # The mock run loop honours the cancel event by itself; nothing to kill.
        return


def build_driver(backend: str, *, gui: bool = False,
                 mock_delay: float = 0.5) -> InstanceDriver:
    """Factory selecting an instance driver by backend name."""
    backend = (backend or "silver").strip().lower()
    if backend == "mock":
        return MockInstanceDriver(mock_delay)
    if backend == "silver":
        return RealInstanceDriver(gui=gui)
    raise ValueError(f"Unknown runner backend: {backend}")


# --------------------------------------------------------------------------- #
# Pooled instance record
# --------------------------------------------------------------------------- #
@dataclass(eq=False)
class PooledInstance:
    """One live, reusable Silver instance managed by the pool.

    ``eq=False`` keeps identity-based hashing/equality so instances can be
    stored in the pool's ``_live`` set (a dataclass with the default
    ``eq=True`` is unhashable).
    """

    uid: int
    handle: Any
    console_log: Optional[Path]
    poisoned: bool = field(default=False)

    def poison(self) -> None:
        self.poisoned = True


# --------------------------------------------------------------------------- #
# The pool
# --------------------------------------------------------------------------- #
class SilverInstancePool:
    """A bounded pool of pre-warmed, reusable Silver instances.

    Parameters
    ----------
    driver:
        Backend driver (:class:`RealInstanceDriver` / :class:`MockInstanceDriver`).
    pool_dir:
        Directory under which each instance gets a stable console-log file
        (``<pool_dir>/inst_<uid>/Console.log``).
    default_sil_getter:
        Callable returning the model path used to pre-warm blank instances, or
        ``None`` when no model is registered yet (pre-warm is then deferred and
        instances are created lazily against the first job's model).
    """

    def __init__(
        self,
        driver: InstanceDriver,
        pool_dir: Path,
        default_sil_getter: Callable[[], Optional[Path]] | None = None,
    ) -> None:
        self._driver = driver
        self._pool_dir = Path(pool_dir)
        self._default_sil_getter = default_sil_getter or (lambda: None)

        self._cond = threading.Condition()
        self._idle: List[PooledInstance] = []
        self._live: set[PooledInstance] = set()  # every created, not-yet-disposed instance
        self._size = 0          # live instances (idle + busy + reserved)
        self._in_use = 0        # currently borrowed
        self._target = 0        # desired number of instances (== license count)
        self._closed = False
        self._uid_seq = itertools.count(1)

    # ---------------------- introspection ----------------------------------- #
    @property
    def is_mock(self) -> bool:
        return self._driver.is_mock

    def stats(self) -> dict:
        with self._cond:
            return {
                "target": self._target,
                "size": self._size,
                "idle": len(self._idle),
                "in_use": self._in_use,
                "closed": self._closed,
            }

    # ---------------------- sizing ------------------------------------------ #
    def set_target(self, target: int) -> None:
        """Set the desired instance count (the licensed concurrency limit).

        Growing is realised lazily (on the next :meth:`prewarm` / :meth:`acquire`);
        shrinking disposes surplus *idle* instances immediately and lets busy
        ones drop off when they are released.
        """
        target = max(0, int(target))
        surplus: List[PooledInstance] = []
        with self._cond:
            self._target = target
            while self._size > self._target and self._idle:
                inst = self._idle.pop()
                self._size -= 1
                surplus.append(inst)
            self._cond.notify_all()
        for inst in surplus:
            self._dispose(inst)

    def prewarm(self) -> int:
        """Eagerly create idle instances up to the current target.

        Returns the number of instances actually created. Safe to call
        repeatedly; already-satisfied targets create nothing. When no default
        model is available yet, creation is skipped and retried later.
        """
        created = 0
        while True:
            with self._cond:
                if self._closed or self._size >= self._target:
                    break
                # Reserve a slot before the slow create so concurrent callers
                # do not over-provision.
                self._size += 1
                uid = next(self._uid_seq)
            sil = self._default_sil_getter()
            if sil is None:
                # No model to open yet; undo the reservation and stop.
                with self._cond:
                    self._size -= 1
                    self._cond.notify_all()
                logger.debug("Pre-warm deferred: no default model registered yet.")
                break
            try:
                inst = self._make_instance(uid, Path(sil))
            except Exception:  # noqa: BLE001
                logger.exception("Failed to pre-warm Silver instance %s", uid)
                with self._cond:
                    self._size -= 1
                    self._cond.notify_all()
                break
            with self._cond:
                self._idle.append(inst)
                self._cond.notify_all()
            created += 1
        if created:
            logger.info("Pre-warmed %d Silver instance(s); pool now %s",
                        created, self.stats())
        return created

    # ---------------------- borrow / return --------------------------------- #
    def acquire(
        self,
        sil_path: Path,
        should_cancel: Callable[[], bool] | None = None,
        poll: float = 0.5,
    ) -> Optional[PooledInstance]:
        """Borrow an idle instance, blocking until one is free.

        A new instance is created lazily (opened on *sil_path*) when the pool is
        below target and none is idle. Returns ``None`` if *should_cancel*
        becomes true while waiting (the job was cancelled before it started).
        """
        should_cancel = should_cancel or (lambda: False)
        while True:
            reserve_uid: Optional[int] = None
            with self._cond:
                if self._closed:
                    return None
                if should_cancel():
                    return None
                if self._idle:
                    inst = self._idle.pop()
                    self._in_use += 1
                    return inst
                if self._size < self._target:
                    self._size += 1
                    reserve_uid = next(self._uid_seq)
                else:
                    self._cond.wait(timeout=poll)
                    continue
            # Slow path: create a new instance outside the lock.
            try:
                inst = self._make_instance(reserve_uid, Path(sil_path))
            except Exception:  # noqa: BLE001
                with self._cond:
                    self._size -= 1
                    self._cond.notify_all()
                raise
            with self._cond:
                self._in_use += 1
            return inst

    def release(self, inst: PooledInstance) -> None:
        """Return a borrowed instance to the pool.

        A healthy instance is kept idle for reuse. A *poisoned* instance (its
        Silver process was force-killed on cancellation) or one made surplus by
        a shrink is disposed; the pool will lazily recreate an instance to meet
        the target on the next demand.
        """
        dispose_it = False
        with self._cond:
            self._in_use = max(0, self._in_use - 1)
            if inst.poisoned or self._size > self._target or self._closed:
                self._size = max(0, self._size - 1)
                dispose_it = True
            else:
                self._idle.append(inst)
            self._cond.notify_all()
        if dispose_it:
            self._dispose(inst)

    def poison(self, inst: PooledInstance) -> None:
        """Mark an instance unusable so :meth:`release` disposes it."""
        inst.poison()

    def force_stop(self, inst: PooledInstance) -> None:
        """Force-terminate the instance's current run (used on cancellation)."""
        self._driver.force_stop(inst.handle)

    def configure_and_run(self, inst: PooledInstance, ctx: RunContext) -> None:
        """Run one job on the borrowed instance (delegates to the driver)."""
        self._driver.configure_and_run(inst.handle, ctx)

    # ---------------------- shutdown ---------------------------------------- #
    def shutdown(self) -> None:
        """Dispose every instance (idle *and* in-use) and release all licenses.

        In-use instances are included so that closing the app never leaves a
        Silver process running with a held license, even if a test was still
        executing at shutdown.
        """
        with self._cond:
            self._closed = True
            self._target = 0
            instances = list(self._live)
            self._idle.clear()
            self._live.clear()
            self._size = 0
            self._in_use = 0
            self._cond.notify_all()
        for inst in instances:
            self._dispose(inst)
        logger.info("Silver instance pool shut down (%d instance(s) disposed)",
                    len(instances))

    # ---------------------- internals --------------------------------------- #
    def _make_instance(self, uid: int, sil_path: Path) -> PooledInstance:
        console_log: Optional[Path] = None
        if not self._driver.is_mock:
            console_log = self._pool_dir / f"inst_{uid}" / "Console.log"
        handle = self._driver.create(Path(sil_path), console_log or Path("mock"))
        logger.info("Created Silver instance uid=%s (mock=%s)", uid, self._driver.is_mock)
        inst = PooledInstance(uid=uid, handle=handle, console_log=console_log)
        with self._cond:
            self._live.add(inst)
        return inst

    def _dispose(self, inst: PooledInstance) -> None:
        with self._cond:
            self._live.discard(inst)
        try:
            self._driver.dispose(inst.handle)
        except Exception:  # noqa: BLE001 - best effort cleanup
            logger.exception("Error disposing Silver instance uid=%s", inst.uid)


# --------------------------------------------------------------------------- #
# Process-wide singleton
# --------------------------------------------------------------------------- #
_pool_lock = threading.Lock()
_pool: Optional[SilverInstancePool] = None


def get_pool(
    driver: InstanceDriver | None = None,
    pool_dir: Path | None = None,
    default_sil_getter: Callable[[], Optional[Path]] | None = None,
) -> SilverInstancePool:
    """Return the process-wide pool, creating it on first use.

    The first caller (normally the worker at start-up) supplies the driver,
    pool directory and default-model getter; later callers just fetch it.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            if driver is None or pool_dir is None:
                raise RuntimeError(
                    "SilverInstancePool has not been initialised yet; the first "
                    "get_pool() call must supply driver and pool_dir."
                )
            _pool = SilverInstancePool(driver, pool_dir, default_sil_getter)
        return _pool


def reset_pool_for_tests() -> None:
    """Dispose and clear the singleton (test helper)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown()
        _pool = None
