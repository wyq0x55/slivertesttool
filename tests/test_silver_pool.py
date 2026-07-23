"""Unit tests for the pre-warmed Silver instance pool.

These tests exercise the pool's concurrency, reuse, resize and poison logic in
isolation using a lightweight fake driver -- no Flask, database or real Silver
installation is required, so they run anywhere.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

try:  # pytest is optional; the module also runs standalone (see __main__).
    import pytest
except Exception:  # noqa: BLE001
    pytest = None

from app.runners.silver_pool import InstanceDriver, SilverInstancePool


class FakeDriver(InstanceDriver):
    """A counting, in-memory driver for deterministic pool tests."""

    is_mock = True

    def __init__(self, create_delay: float = 0.0) -> None:
        self.created = 0
        self.disposed = 0
        self.force_stopped = 0
        self._create_delay = create_delay
        self._lock = threading.Lock()

    def create(self, sil_path, console_log):
        if self._create_delay:
            time.sleep(self._create_delay)
        with self._lock:
            self.created += 1
            handle = {"sil": str(sil_path), "runs": 0, "alive": True}
        return handle

    def configure_and_run(self, handle, ctx):
        # Simulate a short, cancellable run.
        handle["runs"] += 1
        deadline = time.time() + 0.05
        while time.time() < deadline:
            if ctx.cancel_event.is_set():
                from app.runners.silver_runner import RunnerCancelled
                raise RunnerCancelled("cancelled")
            time.sleep(0.005)

    def dispose(self, handle):
        with self._lock:
            self.disposed += 1
            handle["alive"] = False

    def force_stop(self, handle):
        with self._lock:
            self.force_stopped += 1


def _make_pool(driver, target, default=Path("model.sil")):
    pool = SilverInstancePool(driver, Path("/tmp/pool_test"),
                              default_sil_getter=lambda: default)
    pool.set_target(target)
    return pool


def test_prewarm_creates_target_idle_instances():
    driver = FakeDriver()
    pool = _make_pool(driver, target=3)
    warmed = pool.prewarm()
    assert warmed == 3
    assert driver.created == 3
    stats = pool.stats()
    assert stats == {"target": 3, "size": 3, "idle": 3, "in_use": 0, "closed": False}
    # Idempotent: warming again creates nothing.
    assert pool.prewarm() == 0
    assert driver.created == 3


def test_acquire_reuses_instances():
    driver = FakeDriver()
    pool = _make_pool(driver, target=2)
    pool.prewarm()
    for _ in range(10):
        inst = pool.acquire(Path("model.sil"))
        assert inst is not None
        pool.configure_and_run(inst, _ctx())
        pool.release(inst)
    # Only two instances ever created despite ten runs -> reuse works.
    assert driver.created == 2
    assert driver.disposed == 0


def test_lazy_creation_without_prewarm():
    driver = FakeDriver()
    pool = _make_pool(driver, target=2)
    inst = pool.acquire(Path("model.sil"))
    assert driver.created == 1
    pool.release(inst)
    assert pool.stats()["idle"] == 1


def test_concurrency_capped_at_target():
    driver = FakeDriver(create_delay=0.02)
    pool = _make_pool(driver, target=2)

    peak = {"value": 0}
    active = {"value": 0}
    lock = threading.Lock()

    def worker():
        inst = pool.acquire(Path("model.sil"))
        with lock:
            active["value"] += 1
            peak["value"] = max(peak["value"], active["value"])
        time.sleep(0.03)
        with lock:
            active["value"] -= 1
        pool.release(inst)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak["value"] <= 2          # never more than the licensed limit
    assert driver.created <= 2         # at most `target` instances exist


def test_acquire_returns_none_on_cancel():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    # Occupy the only slot.
    held = pool.acquire(Path("model.sil"))
    # A second acquire that is cancelled while waiting returns None promptly.
    result = pool.acquire(Path("model.sil"),
                          should_cancel=lambda: True, poll=0.05)
    assert result is None
    pool.release(held)


def test_poisoned_instance_is_disposed_and_replaced():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    inst = pool.acquire(Path("model.sil"))
    assert driver.created == 1
    pool.poison(inst)
    pool.release(inst)
    assert driver.disposed == 1
    assert pool.stats()["size"] == 0
    # The pool recreates a fresh instance on the next demand (re-grabs license).
    inst2 = pool.acquire(Path("model.sil"))
    assert driver.created == 2
    pool.release(inst2)


def test_shrink_disposes_surplus_idle():
    driver = FakeDriver()
    pool = _make_pool(driver, target=4)
    pool.prewarm()
    assert pool.stats()["idle"] == 4
    pool.set_target(2)
    assert driver.disposed == 2
    assert pool.stats() == {"target": 2, "size": 2, "idle": 2,
                            "in_use": 0, "closed": False}


def test_grow_prewarms_more():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    assert driver.created == 1
    pool.set_target(3)
    pool.prewarm()
    assert driver.created == 3
    assert pool.stats()["idle"] == 3


def test_shutdown_disposes_everything():
    driver = FakeDriver()
    pool = _make_pool(driver, target=3)
    pool.prewarm()
    pool.shutdown()
    assert driver.disposed == 3
    assert pool.stats()["closed"] is True
    # Acquiring from a closed pool yields nothing.
    assert pool.acquire(Path("model.sil"), poll=0.01,
                        should_cancel=lambda: False) is None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ctx():
    from app.runners.silver_runner import RunContext
    return RunContext(
        test_id="TC",
        run_dir=Path("/tmp/run"),
        log_dir=Path("/tmp/logs"),
        sil_path=Path("model.sil"),
        gui=False,
        timeout=10,
        reload_model=True,
    )


def _run_standalone() -> int:
    """Run every ``test_*`` function without pytest (for constrained CIs)."""
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"FAIL {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failures}/{len(funcs)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    if pytest is not None:
        raise SystemExit(pytest.main([__file__, "-v"]))
    raise SystemExit(_run_standalone())
