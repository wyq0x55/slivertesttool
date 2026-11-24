"""执行配置: the project-level clean-room switch and its settings page.

The switch used to be an ephemeral checkbox in the matrix editor topbar: the
editor read it and posted ``clean_room`` on the task body, so a run queued from
anywhere else silently lost it. It is project state now, edited on the
Silver-only 执行配置 settings tab, and the JSON API exposes it next to
``default_signal_list``.
"""

from __future__ import annotations

import pytest

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import AuditLog, LMUser, Project


@pytest.fixture
def sqlite_app(tmp_path):
    class TestCfg(Config):
        TESTING = True
        SECRET_KEY = "exec-config-unit-test-secret-key-32"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'exec.db'}"
        SQLALCHEMY_ENGINE_OPTIONS = {}

    app = create_app(TestCfg)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _lead(app):
    from app.services.lanmatrix import projects_service

    user = LMUser(username="lead_exec", password_hash="x", is_system_admin=True)
    db.session.add(user)
    db.session.commit()
    project = projects_service.create_project(user, code="EXEC01", name="执行配置项目")
    return user, project


def test_default_clean_room_is_project_state_and_audited(sqlite_app):
    from app.services.lanmatrix import projects_service
    from app.services.lanmatrix.errors import ServiceError

    with sqlite_app.app_context():
        user, project = _lead(sqlite_app)

        # Existing projects default to off, and to_dict() carries the field.
        assert project.default_clean_room is False
        assert project.to_dict()["default_clean_room"] is False

        updated = projects_service.update_project(
            user, project, {"default_clean_room": True}
        )
        assert updated.default_clean_room is True
        assert updated.to_dict()["default_clean_room"] is True

        audit_row = (
            AuditLog.query.filter_by(project_id=project.id, action="project.update")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None
        assert audit_row.new_value["default_clean_room"] is True
        assert audit_row.old_value["default_clean_room"] is False

        # The switch is a boolean; strings and numbers are rejected rather than
        # coerced, so a typo in the settings request cannot flip it.
        for bad in ("true", "1", 1, None):
            with pytest.raises(ServiceError):
                projects_service.update_project(
                    user, project, {"default_clean_room": bad}
                )


def test_project_patch_exposes_default_clean_room(sqlite_app):
    with sqlite_app.app_context():
        user, project = _lead(sqlite_app)
        pid, uid = project.id, user.id

    client = sqlite_app.test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = uid
        # Cookie-authenticated writes are rejected without the session CSRF token.
        session["csrf_token"] = "exec-config-csrf"

    response = client.patch(
        f"/api/v1/projects/{pid}",
        json={"changes": {"default_clean_room": True}},
        headers={"X-CSRF-Token": "exec-config-csrf"},
    )
    assert response.status_code == 200
    payload = response.get_json()["data"]["project"]
    assert payload["default_clean_room"] is True

    read_back = client.get(f"/api/v1/projects/{pid}").get_json()["data"]["project"]
    assert read_back["default_clean_room"] is True


def test_exec_config_page_renders_the_switch_for_silver_projects(sqlite_app):
    with sqlite_app.app_context():
        user, project = _lead(sqlite_app)
        pid, uid = project.id, user.id

    client = sqlite_app.test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = uid

    response = client.get(f"/lanmatrix/projects/{pid}/exec")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<h1 class="lm-sr-only">矩阵编辑</h1>' not in html
    assert 'id="lm-clean-room-toggle"' in html
    assert 'id="lm-signal-list-btn"' in html
    assert 'id="lm-ai-generate"' in html
    assert 'id="lm-ai-row-ids"' in html
    assert "js/lanmatrix/editor_qualification.js" in html
    assert f'href="/lanmatrix/projects/{pid}/exec"' in html
    assert '<a class="tab on" href' in html  # the new tab is the active one

    unit_test = Project(
        code="EXEC_UT",
        name="UT Project",
        project_type="unit_test",
        status="active",
        created_by=uid,
    )
    db.session.add(unit_test)
    db.session.commit()
    redirect = client.get(f"/lanmatrix/projects/{unit_test.id}/exec")
    assert redirect.status_code == 302
    assert redirect.headers["Location"].endswith(
        f"/lanmatrix/projects/{unit_test.id}/settings/binding"
    )