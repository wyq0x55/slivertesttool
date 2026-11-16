"""Unit tests for the pre-warmed Silver instance pool.

These tests exercise the pool's concurrency, reuse, resize and poison logic in
isolation using a lightweight fake driver -- no Flask, database or real Silver
installation is required, so they run anywhere.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

try:  # pytest is optional; the module also runs standalone (see __main__).
    import pytest
except Exception:  # noqa: BLE001
    pytest = None

from app.runners.silver_pool import (
    InstanceDriver, MockInstanceDriver, RealInstanceDriver, SilverInstancePool,
)
from app.runners.silver_runner import (
    RunnerError, SilverLaunchCleanupError, SilverRunner,
)


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

    def is_alive(self, handle):
        return handle["alive"]

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
    assert pool.prewarm() == 0
    assert driver.created == 3


def test_failed_disposal_keeps_capacity_and_uid_until_retry():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    instance = pool.acquire(Path("model.sil"))
    original = driver.dispose
    driver.dispose = Mock(side_effect=OSError("injected termination failure"))
    instance.poison()
    pool.release(instance)
    assert instance in pool._live
    assert instance in pool._retiring
    assert pool.stats()["size"] == 1
    assert pool.acquire(Path("model.sil"), blocking=False) is None
    assert driver.created == 1
    driver.dispose = original
    replacement = pool.acquire(Path("model.sil"), blocking=False)
    assert replacement.uid == instance.uid
    assert replacement is not instance
    assert instance not in pool._live
    pool.release(replacement)
    pool.shutdown()


@pytest.mark.parametrize("entry", ["prewarm", "acquire"])
def test_failed_launch_cleanup_keeps_reservation_until_retry(entry):
    class FailedLaunchDriver(FakeDriver):
        def __init__(self):
            super().__init__()
            self.launch_handle = {"alive": True}
            self.fail_launch = True
            self.fail_dispose = True

        def create(self, sil_path, console_log):
            if self.fail_launch:
                self.fail_launch = False
                raise SilverLaunchCleanupError("launch failed", self.launch_handle)
            return super().create(sil_path, console_log)

        def dispose(self, handle):
            if handle is self.launch_handle and self.fail_dispose:
                self.fail_dispose = False
                raise OSError("injected termination failure")
            return super().dispose(handle)

    driver = FailedLaunchDriver()
    pool = _make_pool(driver, target=1)
    if entry == "prewarm":
        assert pool.prewarm() == 0
    else:
        with pytest.raises(RunnerError, match="launch failed"):
            pool.acquire(Path("model.sil"))
    assert pool.stats()["size"] == 1
    assert driver.created == 0
    if entry == "prewarm":
        assert pool.prewarm() == 1
        pool.shutdown()
    else:
        instance = pool.acquire(Path("model.sil"), blocking=False)
        assert instance is not None
        pool.release(instance)
        pool.shutdown()
    assert not driver.launch_handle["alive"]
    assert driver.created == 1


def test_shutdown_retries_failed_cleanup_without_releasing_ownership():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    instance = pool.acquire(Path("model.sil"))
    original = driver.dispose
    driver.dispose = Mock(side_effect=OSError("injected query failure"))
    pool.shutdown()
    assert pool.stats()["size"] == 1
    assert instance in pool._live
    pool.release(instance)
    assert pool.stats()["size"] == 1
    driver.dispose = original
    pool.shutdown()
    assert pool.stats()["size"] == 0
    assert instance not in pool._live
    assert not instance.handle["alive"]


def test_creation_during_shutdown_retains_failed_cleanup():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    original_create = driver.create
    original_dispose = driver.dispose

    def create(sil_path, console_log):
        pool.shutdown()
        return original_create(sil_path, console_log)

    driver.create = create
    driver.dispose = Mock(side_effect=OSError("injected orphan cleanup failure"))
    assert pool.acquire(Path("model.sil")) is None
    assert pool.stats()["size"] == 1
    assert len(pool._live) == 1
    driver.dispose = original_dispose
    pool.shutdown()
    assert pool.stats()["size"] == 0
    assert not pool._live


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


def test_dead_idle_instances_are_replaced_without_capacity_leak():
    driver = FakeDriver()
    pool = _make_pool(driver, target=2)
    pool.prewarm()
    first, second = pool._idle
    first.handle["alive"] = False
    second.handle["alive"] = False
    borrowed = pool.acquire(Path("model.sil"))
    assert borrowed not in (first, second)
    assert driver.disposed == 2
    assert driver.created == 3
    assert pool.stats() == {"target": 2, "size": 1, "idle": 0, "in_use": 1, "closed": False}
    pool.release(borrowed)
    assert pool.stats()["size"] == 1


def test_liveness_probe_error_retires_the_instance():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()

    def fail(handle):
        raise RuntimeError("probe failed")

    driver.is_alive = fail
    replacement = pool.acquire(Path("model.sil"))
    assert driver.disposed == 1
    assert replacement.handle["alive"] is True
    pool.release(replacement)


def test_replacement_creation_failure_preserves_capacity():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    pool._idle[0].handle["alive"] = False

    def fail(sil_path, console_log):
        raise RuntimeError("cannot launch")

    driver.create = fail
    try:
        pool.acquire(Path("model.sil"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("creation failure must propagate")
    assert pool.stats() == {"target": 1, "size": 0, "idle": 0, "in_use": 0, "closed": False}


def test_eviction_cannot_exceed_target_or_dispose_twice():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    pool._idle[0].handle["alive"] = False
    started = threading.Event()
    release_probe = threading.Event()
    original = driver.is_alive

    def pause(handle):
        started.set()
        release_probe.wait(2)
        return original(handle)

    driver.is_alive = pause
    result = {}

    def borrow():
        result["instance"] = pool.acquire(Path("model.sil"))

    borrower = threading.Thread(target=borrow)
    borrower.start()
    assert started.wait(2)
    prewarmer = threading.Thread(target=pool.prewarm)
    prewarmer.start()
    time.sleep(0.05)
    assert driver.created == 1
    release_probe.set()
    borrower.join()
    prewarmer.join()
    assert result["instance"].handle["alive"] is True
    assert driver.disposed == 1
    assert driver.created == 2
    assert pool.stats()["size"] <= 1
    pool.release(result["instance"])


def test_shutdown_during_eviction_disposes_once():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    pool._idle[0].handle["alive"] = False
    started = threading.Event()
    release_probe = threading.Event()

    def pause(handle):
        started.set()
        release_probe.wait(2)
        return False

    driver.is_alive = pause
    result = {}

    def borrow():
        result["instance"] = pool.acquire(Path("model.sil"), poll=0.01)

    borrower = threading.Thread(target=borrow)
    borrower.start()
    assert started.wait(2)
    release_probe.set()
    pool.shutdown()
    borrower.join()
    assert result["instance"] is None
    assert driver.disposed == 1
    assert pool.stats()["size"] == 0


def test_confirmed_dead_silver_cleanup_skips_remote_exit():
    class CallbackServer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Process:
        def poll(self):
            return 1

        def wait(self):
            return 1

    class Handle:
        def __init__(self):
            self.process = Process()
            self.callback_server = CallbackServer()
            self.exit_called = False

        def exit(self, force=False):
            self.exit_called = True

    handle = Handle()
    SilverRunner._safe_dispose(handle)
    assert handle.callback_server.closed is True
    assert handle.exit_called is False


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


def test_dead_idle_instance_is_disposed_before_replacement():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    stale = pool._idle[-1]
    stale.handle["alive"] = False

    replacement = pool.acquire(Path("model.sil"), blocking=False)

    assert replacement is not stale
    assert replacement.handle["alive"]
    assert replacement.uid == stale.uid
    assert driver.created == 2
    assert driver.disposed == 1
    assert pool.stats() == {"target": 1, "size": 1, "idle": 0,
                            "in_use": 1, "closed": False}
    pool.release(replacement)


def test_acquire_discards_multiple_stale_candidates_before_healthy_idle():
    driver = FakeDriver()
    pool = _make_pool(driver, target=3)
    pool.prewarm()
    healthy, dead, poisoned = pool._idle
    dead.handle["alive"] = False
    pool.poison(poisoned)
    driver.is_alive = Mock(wraps=driver.is_alive)

    borrowed = pool.acquire(Path("model.sil"), blocking=False)

    assert borrowed is healthy
    assert driver.created == 3
    assert driver.disposed == 2
    assert driver.is_alive.call_count == 2
    assert pool.stats()["size"] == 1
    pool.release(borrowed)


def test_probe_error_discards_candidate_and_retries():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    stale = pool._idle[-1]
    driver.is_alive = Mock(side_effect=RuntimeError("probe failed"))

    replacement = pool.acquire(Path("model.sil"))

    assert replacement is not stale
    assert driver.created == 2
    assert driver.disposed == 1
    assert not stale.handle["alive"]
    pool.release(replacement)


def test_replacement_creation_failure_propagates_without_retry():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    pool._idle[-1].handle["alive"] = False
    failure = RuntimeError("launch failed")
    driver.create = Mock(side_effect=failure)

    with pytest.raises(RuntimeError) as caught:
        pool.acquire(Path("model.sil"))

    assert caught.value is failure
    driver.create.assert_called_once()
    assert driver.disposed == 1
    assert pool.stats() == {"target": 1, "size": 0, "idle": 0,
                            "in_use": 0, "closed": False}
    driver.create = FakeDriver.create.__get__(driver)
    replacement = pool.acquire(Path("model.sil"))
    assert replacement.uid == 1
    pool.release(replacement)


class BlockingDisposeDriver(FakeDriver):
    def __init__(self):
        super().__init__()
        self.dispose_started = threading.Event()
        self.allow_dispose = threading.Event()
        self.disposing = []

    def dispose(self, handle):
        with self._lock:
            self.disposing.append(handle)
        self.dispose_started.set()
        assert self.allow_dispose.wait(5)
        super().dispose(handle)


def _start_call(callback):
    outcome = {}

    def call():
        try:
            outcome["value"] = callback()
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, outcome


def _finish_call(thread, outcome):
    thread.join(5)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    return outcome["value"]


def test_stale_disposal_holds_capacity_and_uid_until_finished():
    driver = BlockingDisposeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    stale = pool._idle[-1]
    stale.handle["alive"] = False
    thread, outcome = _start_call(lambda: pool.acquire(Path("model.sil")))
    try:
        assert driver.dispose_started.wait(2)
        assert pool.stats()["size"] == 1
        assert pool.stats()["in_use"] == 0
        assert pool.acquire(Path("model.sil"), blocking=False) is None
        assert pool.prewarm() == 0
        assert driver.created == 1
        pool.set_target(2)
        concurrent = pool.acquire(Path("model.sil"), blocking=False)
        assert concurrent.uid != stale.uid
        assert pool.stats()["size"] == 2
    finally:
        driver.allow_dispose.set()
    replacement = _finish_call(thread, outcome)
    assert replacement.uid == stale.uid
    assert replacement.uid != concurrent.uid
    assert len(driver.disposing) == 1
    pool.release(replacement)
    pool.release(concurrent)


def test_shutdown_during_stale_disposal_does_not_dispose_twice():
    driver = BlockingDisposeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    stale = pool._idle[-1]
    stale.handle["alive"] = False
    thread, outcome = _start_call(lambda: pool.acquire(Path("model.sil")))
    try:
        assert driver.dispose_started.wait(2)
        pool.shutdown()
        pool.shutdown()
        pool.release(stale)
        assert pool.stats()["size"] == 1
        assert len(driver.disposing) == 1
    finally:
        driver.allow_dispose.set()
    assert _finish_call(thread, outcome) is None
    assert driver.created == 1
    assert driver.disposed == 1
    assert pool.stats() == {"target": 0, "size": 0, "idle": 0,
                            "in_use": 0, "closed": True}


def test_cancellation_during_stale_disposal_stops_replacement():
    driver = BlockingDisposeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    pool._idle[-1].handle["alive"] = False
    cancelled = threading.Event()
    thread, outcome = _start_call(
        lambda: pool.acquire(Path("model.sil"), should_cancel=cancelled.is_set))
    try:
        assert driver.dispose_started.wait(2)
        cancelled.set()
    finally:
        driver.allow_dispose.set()
    assert _finish_call(thread, outcome) is None
    assert driver.created == 1
    assert driver.disposed == 1
    assert pool.stats()["size"] == 0


def test_shrink_during_stale_disposal_keeps_needed_healthy_instance():
    driver = BlockingDisposeDriver()
    pool = _make_pool(driver, target=2)
    pool.prewarm()
    healthy, stale = pool._idle
    stale.handle["alive"] = False
    thread, outcome = _start_call(lambda: pool.acquire(Path("model.sil")))
    try:
        assert driver.dispose_started.wait(2)
        pool.set_target(1)
        assert len(driver.disposing) == 1
        assert pool.stats()["idle"] == 1
    finally:
        driver.allow_dispose.set()
    assert _finish_call(thread, outcome) is healthy
    pool.release(healthy)
    assert driver.created == 2
    assert driver.disposed == 1
    assert pool.stats() == {"target": 1, "size": 1, "idle": 1,
                            "in_use": 0, "closed": False}


def test_shrink_to_zero_during_stale_disposal_stops_replacement():
    driver = BlockingDisposeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    pool._idle[-1].handle["alive"] = False
    thread, outcome = _start_call(
        lambda: pool.acquire(Path("model.sil"), blocking=False))
    try:
        assert driver.dispose_started.wait(2)
        pool.set_target(0)
    finally:
        driver.allow_dispose.set()
    assert _finish_call(thread, outcome) is None
    assert driver.created == 1
    assert driver.disposed == 1
    assert pool.stats()["size"] == 0


def test_poisoned_release_holds_capacity_and_survives_shutdown():
    driver = BlockingDisposeDriver()
    pool = _make_pool(driver, target=1)
    borrowed = pool.acquire(Path("model.sil"))
    pool.poison(borrowed)
    thread, outcome = _start_call(lambda: pool.release(borrowed))
    try:
        assert driver.dispose_started.wait(2)
        assert pool.stats()["size"] == 1
        assert pool.acquire(Path("model.sil"), blocking=False) is None
        pool.release(borrowed)
        pool.shutdown()
        pool.release(borrowed)
        assert len(driver.disposing) == 1
    finally:
        driver.allow_dispose.set()
    _finish_call(thread, outcome)
    assert driver.disposed == 1
    assert pool.stats()["size"] == 0


def test_shrink_disposal_holds_capacity_and_survives_shutdown():
    driver = BlockingDisposeDriver()
    pool = _make_pool(driver, target=1)
    pool.prewarm()
    thread, outcome = _start_call(lambda: pool.set_target(0))
    try:
        assert driver.dispose_started.wait(2)
        pool.set_target(1)
        assert pool.stats()["size"] == 1
        assert pool.acquire(Path("model.sil"), blocking=False) is None
        pool.shutdown()
        assert len(driver.disposing) == 1
    finally:
        driver.allow_dispose.set()
    _finish_call(thread, outcome)
    assert driver.disposed == 1
    assert pool.stats()["size"] == 0


def test_shutdown_then_late_release_does_not_dispose_or_recycle_uid_twice():
    driver = FakeDriver()
    pool = _make_pool(driver, target=1)
    borrowed = pool.acquire(Path("model.sil"))
    pool.shutdown()
    pool.release(borrowed)
    pool.release(borrowed)
    assert driver.disposed == 1
    assert pool.stats()["size"] == 0
    assert pool._slots.acquire() == borrowed.uid
    assert pool._slots.acquire() != borrowed.uid


def test_real_driver_liveness_uses_local_process_poll_only():
    process = Mock()
    process.poll.return_value = None
    handle = SimpleNamespace(process=process, target=Mock())
    driver = RealInstanceDriver()
    assert driver.is_alive(handle)
    process.poll.return_value = 0
    assert not driver.is_alive(handle)
    process.poll.return_value = 1
    assert not driver.is_alive(handle)
    assert handle.target.mock_calls == []


def test_mock_driver_liveness_tracks_handle_alive():
    driver = MockInstanceDriver(delay_seconds=0)
    handle = driver.create(Path("model.sil"), Path("mock"))
    assert driver.is_alive(handle)
    driver.dispose(handle)
    assert not driver.is_alive(handle)


def test_dead_sdk_cleanup_skips_remote_exit_and_reaps_process():
    process = Mock()
    process.poll.return_value = 1
    handle = SimpleNamespace(
        process=process, callback_server=Mock(), exit=Mock(), target=Mock())
    SilverRunner._safe_dispose(handle)
    handle.exit.assert_not_called()
    assert handle.target.mock_calls == []
    handle.callback_server.close.assert_called_once_with()
    process.wait.assert_called_once_with()


def test_dead_sdk_cleanup_reaps_even_when_callback_close_fails():
    process = Mock()
    process.poll.return_value = 0
    handle = SimpleNamespace(
        process=process, callback_server=Mock(), exit=Mock())
    handle.callback_server.close.side_effect = RuntimeError("callback close failed")
    SilverRunner._safe_dispose(handle)
    handle.exit.assert_not_called()
    process.wait.assert_called_once_with()


def test_live_sdk_cleanup_uses_existing_exit():
    process = Mock()
    process.poll.return_value = None
    handle = SimpleNamespace(process=process, callback_server=Mock(), exit=Mock())
    SilverRunner._safe_dispose(handle)
    handle.exit.assert_called_once_with(force=True)
    handle.callback_server.close.assert_not_called()


def test_sdk_cleanup_probe_error_still_attempts_exit():
    process = Mock()
    process.poll.side_effect = RuntimeError("probe failed")
    handle = SimpleNamespace(process=process, callback_server=Mock(), exit=Mock())
    SilverRunner._safe_dispose(handle)
    handle.exit.assert_called_once_with(force=True)


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
