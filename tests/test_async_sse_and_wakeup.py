from __future__ import annotations

import asyncio
import contextlib
import types

import pytest

from app import create_app
from app.collab import tokens
from app.collab.server import build_asgi_app
from app.config import Config
from app.extensions import db
from app.jobqueue import tasks
from app.models import Project, Task, TaskStatus
from app.services import event_service


@pytest.fixture
def sqlite_app(tmp_path):
    class TestCfg(Config):
        TESTING = True
        SECRET_KEY = "sse-unit-test-secret-key-at-least-32b"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'sse.db'}"
        SQLALCHEMY_ENGINE_OPTIONS = {}
        SSE_POLL_SECONDS = 0.05
        SSE_HEARTBEAT_SECONDS = 5.0

    app = create_app(TestCfg)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_sse_token_is_domain_separated_from_ws_token():
    secret = "shared-secret-key"
    sse_tok = tokens.mint_sse(secret, user_id=1, project_id=2, task_id=3, task_key="T000003")
    ws_tok = tokens.mint(secret, user_id=1, username="alice", project_id=2, role="editor")

    assert tokens.verify_sse(secret, sse_tok) == {
        "uid": 1,
        "pid": 2,
        "tid": 3,
        "key": "T000003",
    }
    assert tokens.verify_sse(secret, ws_tok) is None
    assert tokens.verify(secret, sse_tok) is None


def test_asgi_sse_streams_task_events_and_rejects_bad_tokens(sqlite_app):
    with sqlite_app.app_context():
        project = Project(code="P1", name="Project 1")
        db.session.add(project)
        db.session.commit()
        task = Task(
            task_key="T000001",
            project_id=project.id,
            test_id="TC_01",
            status=TaskStatus.PASSED.value,
            workspace="ws",
            sil_relpath="model.sil",
        )
        db.session.add(task)
        db.session.commit()
        event_service.emit_log(task, "Step 1 ok")
        event_service.emit_result(task, "pass", "Verdict: PASS")
        pid, tid, tkey = project.id, task.id, task.task_key

    asgi = build_asgi_app(sqlite_app)

    async def collect(path: str, query: str):
        sent = []

        async def receive():
            await asyncio.sleep(10)
            return {"type": "http.disconnect"}

        async def send(msg):
            sent.append(msg)

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query.encode("latin-1"),
            "headers": [(b"origin", b"http://localhost:5000")],
        }
        await asgi(scope, receive, send)
        return sent

    bad = asyncio.run(collect(f"/sse/tasks/{pid}/{tkey}", "token=invalid"))
    assert bad[0]["status"] == 401

    good_tok = tokens.mint_sse(
        sqlite_app.config["SECRET_KEY"],
        user_id=1,
        project_id=pid,
        task_id=tid,
        task_key=tkey,
    )
    good = asyncio.run(collect(f"/sse/tasks/{pid}/{tkey}", f"token={good_tok}"))
    assert good[0]["status"] == 200
    headers = dict(good[0]["headers"])
    assert b"text/event-stream" in headers[b"content-type"]
    assert headers[b"access-control-allow-origin"] == b"http://localhost:5000"
    body = b"".join(m.get("body", b"") for m in good[1:]).decode("utf-8")
    assert "Step 1 ok" in body
    assert "Verdict: PASS" in body
    assert "event: end" in body


def test_wait_capacity_honours_reserved_workers(monkeypatch):
    from app.services import license_service

    app = types.SimpleNamespace(
        config_obj=types.SimpleNamespace(HUEY_WORKERS=16, HUEY_RESERVED_WORKERS=4),
        app_context=contextlib.nullcontext,
    )
    monkeypatch.setattr(license_service, "get_limit", lambda: 10)
    assert tasks._wait_capacity(app) == 2


def test_wake_next_queued_run_dispatches_unleased_head(sqlite_app, monkeypatch):
    from app.services import license_service, worker_lock

    with sqlite_app.app_context():
        t1 = Task(
            task_key="T000010",
            test_id="TC_10",
            status=TaskStatus.QUEUED.value,
            run_count=1,
            workspace="ws",
            sil_relpath="m.sil",
        )
        t2 = Task(
            task_key="T000011",
            test_id="TC_11",
            status=TaskStatus.QUEUED.value,
            run_count=2,
            workspace="ws",
            sil_relpath="m.sil",
        )
        db.session.add_all([t1, t2])
        db.session.commit()
        t1_id, t2_id = t1.id, t2.id

    scheduled = []
    monkeypatch.setattr(tasks.huey, "immediate", False)
    monkeypatch.setattr(tasks.huey, "pending", lambda: [])
    monkeypatch.setattr(worker_lock, "ownership_was_lost", lambda: False)
    monkeypatch.setattr(license_service, "is_draining", lambda: False)
    monkeypatch.setattr(
        tasks,
        "dispatch_lease_is_live",
        lambda tid, rc: tid == t1_id,
    )
    monkeypatch.setattr(
        tasks.run_task,
        "schedule",
        lambda args, delay: scheduled.append((args, delay)),
    )

    tasks._wake_next_queued_run(sqlite_app)
    assert scheduled == [((t2_id, 2), 0)]
