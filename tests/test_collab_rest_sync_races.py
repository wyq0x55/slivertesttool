"""PostgreSQL races between accepted PATCH and stale collaboration snapshots."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

from test_collab_rest_sync import sync_env  # noqa: F401 - shared PostgreSQL fixture


def _wait_for_db_lock(engine, pid):
    from sqlalchemy import text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            blocked = connection.execute(text(
                "select cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}).scalar()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError("competing writer did not wait on the shared project lock")


def test_patch_commit_before_stale_materializer_preserves_accepted_cells(sync_env):
    from sqlalchemy import text
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import items_service

    app, uid, pid, ids = sync_env
    started = threading.Event()
    worker_pid = []

    def reconcile():
        with app.app_context():
            worker_pid.append(db.session.execute(text("select pg_backend_pid()")).scalar())
            started.set()
            items_service.materialize_sheet(
                db.session.get(LMUser, uid), db.session.get(Project, pid), "test", [{
                    "uuid": "sync-test", "description": "old", "title": "browser title"}])

    with ThreadPoolExecutor(max_workers=1) as executor:
        with app.app_context():
            row = db.session.get(TestItemRow, ids["test"])
            items_service.update_item(
                db.session.get(LMUser, uid), db.session.get(Project, pid),
                row, row.version, {"description": "accepted REST"}, commit=False)
            future = executor.submit(reconcile)
            assert started.wait(5)
            try:
                _wait_for_db_lock(db.engine, worker_pid[0])
            finally:
                db.session.commit()
        future.result(timeout=10)
    with app.app_context():
        row = db.session.get(TestItemRow, ids["test"])
        assert row.get_field("description") == "accepted REST"
        assert row.title == "browser title", "unrelated browser edits must still land"


def test_materializer_commit_before_patch_enforces_fresh_version(sync_env):
    from sqlalchemy import text
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import items_service
    from app.services.lanmatrix.errors import VersionConflict

    app, uid, pid, ids = sync_env
    started = threading.Event()
    worker_pid = []

    def stale_patch():
        with app.app_context():
            row = db.session.get(TestItemRow, ids["test"])
            worker_pid.append(db.session.execute(text("select pg_backend_pid()")).scalar())
            started.set()
            try:
                items_service.update_item(
                    db.session.get(LMUser, uid), db.session.get(Project, pid),
                    row, 1, {"description": "stale REST"})
            except VersionConflict:
                db.session.rollback()
                return "conflict"
            return "incorrectly accepted"

    with ThreadPoolExecutor(max_workers=1) as executor:
        with app.app_context():
            items_service.materialize_sheet(
                db.session.get(LMUser, uid), db.session.get(Project, pid), "test", [{
                    "uuid": "sync-test", "description": "browser first"}], commit=False)
            future = executor.submit(stale_patch)
            assert started.wait(5)
            try:
                _wait_for_db_lock(db.engine, worker_pid[0])
            finally:
                db.session.commit()
        assert future.result(timeout=10) == "conflict"
    with app.app_context():
        assert db.session.get(TestItemRow, ids["test"]).get_field("description") == "browser first"
