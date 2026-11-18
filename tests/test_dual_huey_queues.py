from __future__ import annotations

import threading

import pytest
from huey.api import Huey
from peewee import SqliteDatabase

from app.config import Config
from app.jobqueue import huey_app, tasks, ut_tasks
from app.jobqueue.huey_app import _BootstrapOwnedSqlStorage, aux_huey, huey


def test_huey_and_aux_huey_have_distinct_queue_names():
    assert huey is not aux_huey
    assert huey.name == Config.HUEY_NAME == "silvetestapp"
    assert aux_huey.name == Config.HUEY_AUX_NAME == "silvetestapp_aux"


def test_silver_and_aux_tasks_register_on_isolated_queues():
    assert tasks.run_task.huey is huey
    assert tasks.run_ai_generation.huey is aux_huey
    assert tasks.run_ai_generation_task.huey is aux_huey
    assert ut_tasks.ut_generate_batch.huey is aux_huey
    assert ut_tasks.ut_advance_job.huey is aux_huey
    assert ut_tasks.run_ut_batch.huey is aux_huey
    assert ut_tasks.run_ut_job.huey is aux_huey

    silver_registered = set(huey._registry._registry.values())
    aux_registered = set(aux_huey._registry._registry.values())

    assert tasks.run_task.task_class in silver_registered
    assert tasks.run_task.task_class not in aux_registered

    for aux_wrapper in (
        tasks.run_ai_generation_task,
        ut_tasks.run_ut_batch,
        ut_tasks.run_ut_job,
    ):
        assert aux_wrapper.task_class in aux_registered
        assert aux_wrapper.task_class not in silver_registered

    periodic_classes = {type(t) for t in huey._registry.periodic_tasks}
    assert tasks.recover_run_attempts_job.task_class in periodic_classes
    assert tasks.recover_ai_generation_job.task_class in periodic_classes
    assert tasks.prune_task_events_job.task_class in periodic_classes
    assert aux_huey._registry.periodic_tasks == []


def test_sql_storage_physically_isolates_silver_backlog_from_aux_queue(tmp_path, monkeypatch):
    sqlite_db = SqliteDatabase(str(tmp_path / "dual_huey.db"))
    try:
        silver_q = Huey(
            name="silvetestapp",
            storage_class=_BootstrapOwnedSqlStorage,
            database=sqlite_db,
            immediate=False,
        )
        aux_q = Huey(
            name="silvetestapp_aux",
            storage_class=_BootstrapOwnedSqlStorage,
            database=sqlite_db,
            immediate=False,
        )
        monkeypatch.setattr(huey_app, "huey", silver_q)
        monkeypatch.setattr(huey_app, "aux_huey", aux_q)
        # Idempotent dual initialization from explicit bootstrap owner.
        huey_app.ensure_huey_schema()
        huey_app.ensure_huey_schema()

        @silver_q.task()
        def sim_job(idx: int) -> int:
            return idx

        @aux_q.task()
        def compile_ut(idx: int) -> str:
            return f"ut-{idx}"

        for i in range(20):
            sim_job(i)

        assert silver_q.storage.queue_size() == 20
        assert aux_q.storage.queue_size() == 0
        assert aux_q.dequeue() is None

        compile_ut(99)
        assert aux_q.storage.queue_size() == 1
        assert silver_q.storage.queue_size() == 20

        dequeued = aux_q.dequeue()
        assert dequeued is not None
        assert isinstance(dequeued, compile_ut.task_class)
        assert dequeued.args == (99,)
        assert aux_q.dequeue() is None
        assert silver_q.storage.queue_size() == 20
    finally:
        sqlite_db.close()


def test_aux_consumer_runs_cleanly_on_background_thread(tmp_path):
    sqlite_db = SqliteDatabase(
        str(tmp_path / "aux_thread.db"),
         pragmas={"journal_mode": "wal"},
        check_same_thread=False,
    )
    done = threading.Event()
    observed: list[int] = []
    try:
        aux_q = Huey(
            name="silvetestapp_aux",
            storage_class=_BootstrapOwnedSqlStorage,
            database=sqlite_db,
            immediate=False,
        )
        aux_q.storage.initialize_tables()

        @aux_q.task()
        def quick_aux(val: int) -> None:
            observed.append(val)
            done.set()

        consumer = aux_q.create_consumer(workers=1, worker_type="thread", periodic=False)
        consumer._set_signal_handlers = lambda: None
        thread = threading.Thread(target=consumer.run, name="huey-aux-consumer", daemon=True)
        thread.start()
        try:
            quick_aux(7)
            assert done.wait(timeout=5.0)
            assert observed == [7]
        finally:
            consumer.stop(graceful=True)
            thread.join(timeout=5.0)
            assert not thread.is_alive()
    finally:
        sqlite_db.close()


def test_run_worker_cli_parser_accepts_queue_roles(monkeypatch):
    import run_worker

    monkeypatch.delenv("WORKER_QUEUE_ROLE", raising=False)
    assert run_worker.build_parser().parse_args([]).queue == "all"
    assert run_worker.build_parser().parse_args(["--queue", "all"]).queue == "all"
    assert run_worker.build_parser().parse_args(["--queue", "silver"]).queue == "silver"
    assert run_worker.build_parser().parse_args(["--queue", "aux"]).queue == "aux"

    monkeypatch.setenv("WORKER_QUEUE_ROLE", "aux")
    assert run_worker.build_parser().parse_args([]).queue == "aux"
    assert run_worker.build_parser().parse_args(["--queue", "silver"]).queue == "silver"

    with pytest.raises(SystemExit):
        run_worker.build_parser().parse_args(["--queue", "invalid"])
