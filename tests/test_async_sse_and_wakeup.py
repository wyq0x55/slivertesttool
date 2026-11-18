from __future__ import annotations

import asyncio
import contextlib
import socket
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


def test_resolve_sse_gateway_base_auto_derivation_and_testing_guard(sqlite_app, monkeypatch):
    from app.routes.lanmatrix import tasks as task_routes

    task_routes._sse_probe_cache.clear()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        open_port = listener.getsockname()[1]

        # 1) Under TESTING=True without explicit COLLAB_SSE_BASE_URL, never auto-redirect.
        with sqlite_app.test_request_context(
            "/api/v1/projects/1/tasks/T000001/stream",
            base_url="http://192.168.10.25:8080",
        ):
            cfg_testing = types.SimpleNamespace(
                TESTING=True,
                COLLAB_SSE_BASE_URL="",
                COLLAB_SSE_AUTO=True,
                COLLAB_PORT=open_port,
                COLLAB_WS_URL="",
            )
            from flask import request

            assert task_routes._resolve_sse_gateway_base(cfg_testing, request) == ""

            # Explicit COLLAB_SSE_BASE_URL overrides TESTING=True.
            cfg_explicit = types.SimpleNamespace(
                TESTING=True,
                COLLAB_SSE_BASE_URL="http://10.0.0.8:8765/",
                COLLAB_SSE_AUTO=True,
                COLLAB_PORT=open_port,
                COLLAB_WS_URL="",
            )
            assert (
                task_routes._resolve_sse_gateway_base(cfg_explicit, request)
                == "http://10.0.0.8:8765"
            )

        # 2) Non-testing + reachable local collab port preserves LAN host and IPv6 brackets.
        monkeypatch.setitem(sqlite_app.config, "TESTING", False)
        with sqlite_app.test_request_context(
            "/api/v1/projects/1/tasks/T000001/stream",
            base_url="http://192.168.10.25:8080",
        ):
            from flask import request

            cfg_lan = types.SimpleNamespace(
                TESTING=False,
                COLLAB_SSE_BASE_URL="",
                COLLAB_SSE_AUTO=True,
                COLLAB_PORT=open_port,
                COLLAB_WS_URL="",
            )
            assert (
                task_routes._resolve_sse_gateway_base(cfg_lan, request)
                == f"http://192.168.10.25:{open_port}"
            )

            cfg_ws = types.SimpleNamespace(
                TESTING=False,
                COLLAB_SSE_BASE_URL="",
                COLLAB_SSE_AUTO=True,
                COLLAB_PORT=open_port,
                COLLAB_WS_URL="wss://collab.lan.local:9443/",
            )
            assert (
                task_routes._resolve_sse_gateway_base(cfg_ws, request)
                == "https://collab.lan.local:9443"
            )

        with sqlite_app.test_request_context(
            "/api/v1/projects/1/tasks/T000001/stream",
            base_url="https://[::1]:8443",
        ):
            from flask import request

            cfg_v6 = types.SimpleNamespace(
                TESTING=False,
                COLLAB_SSE_BASE_URL="",
                COLLAB_SSE_AUTO=True,
                COLLAB_PORT=open_port,
                COLLAB_WS_URL="",
            )
            assert (
                task_routes._resolve_sse_gateway_base(cfg_v6, request)
                == f"https://[::1]:{open_port}"
            )

    # 3) Once listener is closed (and probe cache cleared), falls back to "" cleanly.
    task_routes._sse_probe_cache.clear()
    with sqlite_app.test_request_context(
        "/api/v1/projects/1/tasks/T000001/stream",
        base_url="http://192.168.10.25:8080",
    ):
        from flask import request

        cfg_closed = types.SimpleNamespace(
            TESTING=False,
            COLLAB_SSE_BASE_URL="",
            COLLAB_SSE_AUTO=True,
            COLLAB_PORT=open_port,
            COLLAB_WS_URL="",
        )
        assert task_routes._resolve_sse_gateway_base(cfg_closed, request) == ""


def test_task_service_persists_clean_room_and_project_default_signal_list(sqlite_app, monkeypatch):
    from unittest.mock import Mock

    from app.models import AuditLog, LMUser
    from app.runners import test_runner
    from app.services import license_service, task_service, worker_lock
    from app.services.lanmatrix import projects_service
    from app.services.lanmatrix.errors import ServiceError

    with sqlite_app.app_context():
        user = LMUser(username="lead", password_hash="x", is_system_admin=True)
        db.session.add(user)
        db.session.commit()

        project = projects_service.create_project(user, code="CR01", name="Clean Room Proj")
        updated = projects_service.update_project(
            user,
            project,
            {"default_signal_list": "  EngineSpeed \r\n\r\n VehicleSpeed  \n"},
        )
        assert updated.default_signal_list == "EngineSpeed\nVehicleSpeed"
        audit_row = (
            AuditLog.query.filter_by(project_id=project.id, action="project.update")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None
        assert audit_row.new_value["default_signal_list"] == "EngineSpeed\nVehicleSpeed"

        with pytest.raises(ServiceError):
            projects_service.update_project(
                user, project, {"default_signal_list": "s" * (256 * 1024 + 10)}
            )

        created = task_service.create_task(
            task_name="TC_CR_1",
            file_name="(json runner)",
            submitter="lead",
            test_id="TC_CR_1",
            sil_relpath="model.sil",
            workspace="ws",
            project_id=project.id,
            submitter_id=user.id,
            clean_room=True,
        )
        assert created.clean_room is True
        created.status = TaskStatus.PASSED.value
        db.session.commit()

        upserted, started = task_service.upsert_task(
            task_name="TC_CR_1",
            file_name="(json runner)",
            submitter="lead",
            test_id="TC_CR_1",
            sil_relpath="model.sil",
            workspace="ws",
            project_id=project.id,
            submitter_id=user.id,
            clean_room=False,
        )
        assert started is True
        assert upserted.id == created.id
        assert upserted.clean_room is False
        upserted.status = TaskStatus.FAILED.value
        db.session.commit()

        requeued = task_service.requeue_task(
            upserted,
            sil_relpath="model.sil",
            workspace="ws",
            submitter="lead",
            submitter_id=user.id,
            clean_room=True,
        )
        assert requeued.clean_room is True

        # Verify _run_task_pooled forwards per-task clean_room=True to pool.acquire.
        pool = Mock()
        instance = object()
        pool.acquire.return_value = instance
        monkeypatch.setattr(tasks, "get_pool", lambda *_args: pool)
        monkeypatch.setattr(worker_lock, "ownership_was_lost", lambda: False)
        monkeypatch.setattr(
            worker_lock,
            "ownership_guard",
            lambda: contextlib.nullcontext(True),
        )
        monkeypatch.setattr(license_service, "is_draining", lambda: False)
        monkeypatch.setattr(license_service, "try_acquire", lambda: True)
        monkeypatch.setattr(license_service, "release", lambda: None)
        monkeypatch.setattr(test_runner, "execute", lambda *_a, **_kw: None)

        tasks._run_task_pooled(sqlite_app, sqlite_app.config_obj, requeued.id, requeued.run_count)
        assert pool.acquire.call_args.kwargs.get("clean_room") is True
