from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url


@pytest.fixture(params=["Asia/Shanghai", "America/New_York"])
def nonutc_app(app_ctx, request):
    from app import create_app
    from app.extensions import db

    config = app_ctx.config_obj
    database_url = make_url(config.SQLALCHEMY_DATABASE_URI)
    options = f"-c timezone={request.param} -c application_name=time-contract"
    database_url = database_url.update_query_dict({"options": options})

    class NonUtcConfig(config):
        SQLALCHEMY_DATABASE_URI = database_url

    application = create_app(NonUtcConfig)
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()


def test_postgresql_connections_remain_utc_after_rollback_and_reconnect(nonutc_app):
    from app.extensions import db

    with nonutc_app.app_context():
        for _attempt in range(2):
            assert db.session.scalar(text("SHOW timezone")) == "UTC"
            assert db.session.scalar(text("SHOW application_name")) == "time-contract"
            db.session.rollback()
            assert db.session.scalar(text("SHOW timezone")) == "UTC"
            db.session.remove()
            db.engine.dispose()


def test_task_and_event_defaults_roundtrip_as_utc(nonutc_app):
    from app.extensions import db
    from app.models import Task, TaskEvent

    with nonutc_app.app_context():
        before = datetime.now(timezone.utc)
        task = Task(task_key="TIME0001")
        db.session.add(task)
        db.session.flush()
        event = TaskEvent(task_id=task.id, message="time contract")
        db.session.add(event)
        db.session.commit()
        db.session.refresh(task)
        db.session.refresh(event)
        after = datetime.now(timezone.utc)
        for record in (task, event):
            assert before <= record.created_at.replace(tzinfo=timezone.utc) <= after
            serialized = datetime.fromisoformat(record.to_dict()["created_at"].replace("Z", "+00:00"))
            assert before - timedelta(seconds=1) <= serialized <= after


def test_import_expiry_roundtrips_without_session_offset(nonutc_app):
    from app.extensions import db
    from app.models import DataJob, Project

    with nonutc_app.app_context():
        project = Project(code="TIME", name="Time contract")
        db.session.add(project)
        db.session.flush()
        deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
        job = DataJob(project_id=project.id, job_type="import", expires_at=deadline)
        db.session.add(job)
        db.session.commit()
        db.session.refresh(job)
        assert job.expires_at.replace(tzinfo=timezone.utc) == deadline


def test_stale_collaboration_presence_does_not_block_rest(nonutc_app):
    from app.collab import presence
    from app.extensions import db
    from app.models import CollabPresence, Project

    with nonutc_app.app_context():
        project = Project(code="PRESENCE", name="Presence contract")
        db.session.add(project)
        db.session.flush()
        stale = datetime.now(timezone.utc) - timedelta(seconds=120)
        db.session.add(CollabPresence(project_id=project.id, connections=1, updated_at=stale))
        db.session.commit()
        db.session.expire_all()
        assert presence.is_collab_active(project.id, ttl_seconds=30) is False
        assert project.id not in presence.active_project_ids(ttl_seconds=30)


def test_account_lockout_expires_at_configured_deadline(nonutc_app, monkeypatch):
    from app.extensions import db
    from app.models import LMUser
    from app.routes.lanmatrix import auth

    started = datetime.now(timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time.astimezone(tz) if tz is not None else current_time.replace(tzinfo=None)

    current_time = started
    monkeypatch.setattr(auth, "_dt", SimpleNamespace(
        datetime=FrozenDatetime, timedelta=timedelta, timezone=timezone,
    ))
    with nonutc_app.app_context():
        user = LMUser(username="time-contract-user", status="active")
        user.set_password("test-time-password")
        db.session.add(user)
        db.session.commit()
    client = nonutc_app.test_client()
    for _attempt in range(auth._LOCK_THRESHOLD):
        response = client.post("/api/v1/auth/login", json={
            "username": "time-contract-user", "password": "wrong-password",
        })
        assert response.status_code == 401
    response = client.post("/api/v1/auth/login", json={
        "username": "time-contract-user", "password": "test-time-password",
    })
    assert response.status_code == 423
    current_time = started + timedelta(minutes=auth._LOCK_MINUTES, seconds=1)
    response = client.post("/api/v1/auth/login", json={
        "username": "time-contract-user", "password": "test-time-password",
    })
    assert response.status_code == 200
