"""Controlled-clock timing contracts, independent of engines and databases."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.ut_pipeline import timing


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(utc="2026-10-26T00:00:00+00:00", ticks=0, commits=0)

    def commit():
        state.commits += 1

    monkeypatch.setattr(timing, "now", lambda: state.utc)
    monkeypatch.setattr(timing.time, "monotonic_ns", lambda: state.ticks)
    monkeypatch.setattr(timing, "db", SimpleNamespace(session=SimpleNamespace(
        is_active=True, commit=commit, rollback=lambda: None)))
    return state


def job(**values):
    return SimpleNamespace(generation={"existing": "keep"}, current_attempt_id=7,
                           batch_id=2, state="generating", superseded_by=None, **values)


def test_state_waits_freeze_and_repeated_state_does_not_reset(clock):
    item = job()
    timing.initialize(item)
    clock.utc = "2026-10-26T00:00:03+00:00"
    timing.transition(item, "generating")
    clock.utc = "2026-10-26T00:00:07+00:00"
    timing.transition(item, "generating")
    timing.transition(item, "awaiting_review")
    clock.utc = "2026-10-26T00:00:09+00:00"
    timing.transition(item, "approved")
    rows = timing.snapshot(item, {})["steps"]
    assert [row["duration_ms"] for row in rows] == [3000, 4000, 2000]
    assert [row["kind"] for row in rows] == ["wait", "group", "wait"]
    assert item.generation["existing"] == "keep" and clock.commits == 0


def test_nested_calls_persist_start_and_freeze_monotonic_even_if_wall_clock_moves(clock):
    item = job()
    attempt = SimpleNamespace(id=7, seq=2, csv_sha256="a" * 64)
    with timing.stage(item, "host_workflow", attempt=attempt, kind="group"):
        assert item.generation[timing.KEY]["steps"][0]["status"] == "running"
        assert clock.commits == 1
        clock.ticks = 1_000_000
        with timing.stage(item, "stub_model", attempt=attempt, round_number=1):
            clock.utc = "2026-10-25T23:00:00+00:00"
            clock.ticks = 4_000_000
        clock.ticks = 5_000_000
    rows = timing.snapshot(item, {})["steps"]
    assert [row["duration_ms"] for row in rows] == [5, 3]
    assert rows[1]["attempt_seq"] == 2 and rows[1]["round"] == 1
    assert rows[1]["candidate_sha256"] == "a" * 64
    assert clock.commits == 4


def test_failed_requests_are_separate_records_without_consuming_workflow_round(clock):
    item = job()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="offline"), timing.stage(item, "input_model", round_number=1):
            clock.ticks += 6_000_000
            raise RuntimeError("offline")
    rows = timing.snapshot(item, {})["steps"]
    assert [row["id"] for row in rows] == [1, 2]
    assert all(row["status"] == "failed" and row["duration_ms"] == 6 for row in rows)
    assert all(row["round"] == 1 for row in rows)


def test_read_is_nonmutating_legacy_history_and_running_estimate_are_explicit(clock):
    item = job()
    original = deepcopy(item.generation)
    assert timing.snapshot(item, {})["history_complete"] is False
    assert item.generation == original
    timing.initialize(item)
    clock.utc = "2026-10-26T00:00:02+00:00"
    original = deepcopy(item.generation)
    snap = timing.snapshot(item, {})
    assert snap["steps"][0]["elapsed_ms"] == 2000 and snap["steps"][0]["provisional"]
    assert item.generation == original


def test_recovery_does_not_invent_downtime_duration_and_superseded_wait_stops(clock):
    item = job()
    timing.initialize(item)
    timing.interrupt(item)
    row = timing.snapshot(item, {})["steps"][0]
    assert (row["status"], row["duration_ms"], row["ended_at"]) == ("interrupted", None, None)
    assert timing.snapshot(item, {})["history_complete"] is False
    timing.transition(item, "human_wait")
    timing.transition(item, "awaiting_review")
    clock.utc = "2026-10-26T00:00:08+00:00"
    timing.supersede(item)
    assert timing.snapshot(item, {})["steps"][-1]["outcome"] == "superseded"
    clock.utc = "2026-10-26T00:02:00+00:00"
    assert timing.snapshot(item, {})["steps"][-1]["elapsed_ms"] == 8000


def test_shared_generation_has_its_own_clock_and_stale_terminal_spans_are_unknown(clock):
    item = job()
    phase = {"phase": "solving", "status": "running", "elapsed_ms": 10}
    shared = {"phase": "extracting", "status": "running", "elapsed_ms": 20}
    progress = {"project": {"updated_at": "project-time", "timings": [shared]},
                "function": {"updated_at": "function-time", "timings": [phase]}}
    snap = timing.snapshot(item, progress)
    assert snap["generation"]["project_updated_at"] == "project-time"
    item.state = "error"
    snap = timing.snapshot(item, progress)
    assert all(row["status"] == "interrupted" for key in ("shared", "function")
               for row in snap["generation"][key])
    assert phase["status"] == shared["status"] == "running"


def test_backwards_wall_clock_does_not_produce_negative_duration(clock):
    item = job()
    timing.initialize(item)
    clock.utc = "2026-10-25T23:59:59+00:00"
    timing.transition(item, "generating")
    row = timing.snapshot(item, {})["steps"][0]
    assert row["duration_ms"] is None and row["duration_source"] == "unknown"


def test_final_generation_phases_survive_progress_file_cleanup(clock):
    item = job()
    item.state = "awaiting_review"
    interval = {"phase": "solving", "status": "completed", "duration_ms": 345}
    item.generation.update(phase_timings=[interval], timings=[{
        "phase": "single_function_generation", "status": "completed", "duration_ms": 400}])
    rows = timing.snapshot(item, {})["generation"]["function"]
    assert rows[0]["kind"] == "group"
    assert rows[1] == interval
    assert "kind" not in item.generation["timings"][0]
    shared = {"phase": "extracting", "status": "completed", "duration_ms": 1234}
    timing.record_generation(item, {"project": {"timings": [shared]}})
    assert timing.snapshot(item, {})["generation"]["shared"] == [shared]
