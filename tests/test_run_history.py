"""Project-scoped, read-only history for matrix-backed test runs."""
from __future__ import annotations

import importlib
import os
import tempfile
from datetime import datetime, timedelta

import pytest


@pytest.fixture(scope="module")
def history_app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old = {
        name: os.environ.get(name)
        for name in ("DATABASE_URL", "TEST_DATABASE_URL", "SECRET_KEY")
    }
    os.environ.pop("TEST_DATABASE_URL", None)
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["SECRET_KEY"] = "run-history-test-key"

    import app.config as config_mod
    import app as app_pkg

    importlib.reload(config_mod)
    importlib.reload(app_pkg)

    application = app_pkg.create_app()
    from app.bootstrap import bootstrap_app

    bootstrap_app(application)
    try:
        yield application
    finally:
        from app.extensions import db

        with application.app_context():
            db.session.remove()
            db.engine.dispose()
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def history_env(history_app):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, TestRunRecord

    with history_app.app_context():
        suffix = datetime.utcnow().strftime("%H%M%S%f")
        reader = LMUser(username=f"hr{suffix}", display_name="Reader", password_hash="x")
        outsider = LMUser(username=f"ho{suffix}", display_name="Outsider", password_hash="x")
        db.session.add_all([reader, outsider])
        db.session.commit()

        project = Project(code=f"H{suffix}"[:16], name="History", owner_id=reader.id)
        other = Project(code=f"O{suffix}"[:16], name="Other", owner_id=reader.id)
        db.session.add_all([project, other])
        db.session.commit()
        db.session.add(ProjectMember(project_id=project.id, user_id=reader.id, role="reader"))
        db.session.commit()

        env = {
            "app": history_app,
            "client": history_app.test_client(),
            "project": project,
            "other": other,
            "reader": reader,
            "outsider": outsider,
        }
        yield env

        TestRunRecord.query.filter(
            TestRunRecord.project_id.in_([project.id, other.id])
        ).delete(synchronize_session=False)
        ProjectMember.query.filter(
            ProjectMember.project_id.in_([project.id, other.id])
        ).delete(synchronize_session=False)
        db.session.delete(project)
        db.session.delete(other)
        db.session.delete(reader)
        db.session.delete(outsider)
        db.session.commit()


def _record(env, *, project=None, test_id="TC-1", verdict="PASS",
            outcome="pass", model_name="ecu", model_version="v1",
            row_uuid="row-1", hours=0, task_key="T000321"):
    from app.extensions import db
    from app.models import TestRunRecord

    record = TestRunRecord(
        project_id=(project or env["project"]).id,
        row_uuid=row_uuid,
        test_id=test_id,
        task_key=task_key,
        verdict=verdict,
        outcome=outcome,
        model_name=model_name,
        model_version=model_version,
        executor_id=env["reader"].id,
        executor_name="Reader",
        executed_at=datetime(2026, 3, 1, 9, 0, 0) + timedelta(hours=hours),
        executed_on="2026-03-01",
    )
    db.session.add(record)
    db.session.commit()
    return record


def _login(client, user_id):
    with client.session_transaction() as session:
        session["lm_user_id"] = user_id


def _url(project_id):
    return f"/api/v1/projects/{project_id}/test-run-history"


def test_run_history_route_is_registered(history_app):
    rules = [
        rule for rule in history_app.url_map.iter_rules()
        if rule.rule == "/api/v1/projects/<int:project_id>/test-run-history"
    ]
    assert len(rules) == 1
    assert "GET" in rules[0].methods
    assert rules[0].endpoint == "lanmatrix_projects.test_run_history"


def test_reader_can_page_exact_project_and_test_history(history_env):
    older = _record(history_env, verdict="PASS", outcome="pass", hours=1)
    newer = _record(
        history_env, verdict="FAIL", outcome="fail", model_version="v2", hours=2,
    )
    _record(history_env, test_id="TC-2", row_uuid="row-2", hours=3)
    _record(
        history_env, project=history_env["other"], verdict="ERROR",
        outcome="error", row_uuid="foreign", hours=4,
    )

    client = history_env["client"]
    project_id = history_env["project"].id
    url = _url(project_id)
    _login(client, history_env["reader"].id)

    first = client.get(url, query_string={"test_id": "TC-1", "page_size": 1})
    assert first.status_code == 200
    body = first.get_json()
    assert body["success"] is True
    data = body["data"]
    assert set(data) == {"test_id", "page", "page_size", "total", "items"}
    assert data["test_id"] == "TC-1"
    assert data["page"] == 1
    assert data["page_size"] == 1
    assert data["total"] == 2
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item == {
        "id": newer.id,
        "row_uuid": "row-1",
        "test_id": "TC-1",
        "verdict": "FAIL",
        "outcome": "fail",
        "model_name": "ecu",
        "model_version": "v2",
        "executor_name": "Reader",
        "executed_at": "2026-03-01T11:00:00",
        "executed_on": "2026-03-01",
    }
    assert "task_key" not in item
    assert "executor_id" not in item

    second = client.get(
        url, query_string={"test_id": "TC-1", "page": 2, "page_size": 1},
    )
    assert second.status_code == 200
    page = second.get_json()["data"]
    assert page["total"] == 2
    assert [entry["id"] for entry in page["items"]] == [older.id]


def test_run_history_requires_project_view_and_valid_case_id(history_env):
    client = history_env["client"]
    project_id = history_env["project"].id
    url = _url(project_id)

    assert client.get(url, query_string={"test_id": "TC-1"}).status_code == 401

    _login(client, history_env["outsider"].id)
    assert client.get(url, query_string={"test_id": "TC-1"}).status_code == 403

    _login(client, history_env["reader"].id)
    assert client.get(url).status_code == 400
    assert client.get(url, query_string={"test_id": "   "}).status_code == 400
    assert client.get(
        url, query_string={"test_id": "x" * 129},
    ).status_code == 400
    assert client.get(
        url, query_string={"test_id": "TC-1", "page": 0},
    ).status_code == 400
    assert client.get(
        url, query_string={"test_id": "TC-1", "page_size": 501},
    ).status_code == 400
    assert client.get(
        _url(history_env["project"].id + 99999),
        query_string={"test_id": "TC-1"},
    ).status_code == 404


def test_run_history_empty_page_is_successful(history_env):
    _login(history_env["client"], history_env["reader"].id)
    response = history_env["client"].get(
        _url(history_env["project"].id),
        query_string={"test_id": "missing"},
    )
    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "test_id": "missing",
        "page": 1,
        "page_size": 100,
        "total": 0,
        "items": [],
    }


def test_page_beyond_history_returns_empty_without_overflow(history_env):
    _record(history_env)
    client = history_env["client"]
    _login(client, history_env["reader"].id)
    response = client.get(
        _url(history_env["project"].id),
        query_string={"test_id": "TC-1", "page": str(10 ** 30), "page_size": 1},
    )
    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "test_id": "TC-1",
        "page": 10 ** 30,
        "page_size": 1,
        "total": 1,
        "items": [],
    }


def test_dashboard_exposes_lazy_run_history_panel():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "app/templates/lanmatrix/dashboard.html").read_text(encoding="utf-8")
    js = (root / "app/static/js/lanmatrix/dashboard.js").read_text(encoding="utf-8")
    assert 'id="lm-vc-history"' in html
    assert "test-run-history" in js
    assert "data-history-test-id" in js


def test_dashboard_run_history_ignores_stale_responses_and_reports_expired_login():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/js/lanmatrix/dashboard.js").read_text(encoding="utf-8")
    assert "requestId !== historyRequestId" in js
    assert "登录已过期" in js
