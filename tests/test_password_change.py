"""Forced password change and the self-service password endpoint."""

import pytest


@pytest.fixture()
def app(app_ctx):
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import LMUser

        user = LMUser(username="newbie", display_name="Newbie", must_change_password=True)
        user.set_password("temporary-pass")
        db.session.add(user)
        db.session.commit()
    return app_ctx


def _login(client, username="newbie", password="temporary-pass"):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def test_flagged_session_cannot_use_the_api_until_the_password_changes(app):
    client = app.test_client()
    data = _login(client)
    assert data["user"]["must_change_password"] is True
    blocked = client.get("/api/v1/config")
    assert blocked.status_code == 403
    assert blocked.get_json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    page = client.get("/lanmatrix/home", follow_redirects=False)
    assert page.status_code in (302, 303)
    assert "/lanmatrix/password" in page.headers["Location"]

    csrf = data["csrf_token"]
    rejected = client.post("/api/v1/me/password", json={
        "current_password": "temporary-pass", "new_password": "temporary-pass"},
        headers={"X-CSRF-Token": csrf})
    assert rejected.status_code == 400

    changed = client.post("/api/v1/me/password", json={
        "current_password": "temporary-pass", "new_password": "replaced-pass"},
        headers={"X-CSRF-Token": csrf})
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()["data"]["user"]["must_change_password"] is False
    assert client.get("/api/v1/config").status_code == 200
    assert client.post("/api/v1/auth/login", json={
        "username": "newbie", "password": "temporary-pass"}).status_code == 401


def test_api_token_cannot_change_a_password(app):
    from app.extensions import db
    from app.models import LMUser
    from app.services import api_token_service

    with app.app_context():
        user = LMUser.query.filter_by(username="newbie").one()
        _row, secret = api_token_service.create(user, "cli", 30)
        db.session.commit()
    client = app.test_client()
    response = client.post("/api/v1/me/password", json={
        "current_password": "temporary-pass", "new_password": "replaced-pass"},
        headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 403


@pytest.mark.parametrize("current_password,new_password", [
    ("wrong-pass", "replaced-pass"), ("temporary-pass", "short"),
    (["temporary-pass"], "replaced-pass"), ("temporary-pass", ["replaced-pass"]),
])
def test_invalid_change_preserves_password_and_flag(app, current_password, new_password):
    client = app.test_client()
    data = _login(client)
    response = client.post("/api/v1/me/password", json={
        "current_password": current_password, "new_password": new_password},
        headers={"X-CSRF-Token": data["csrf_token"]})
    assert response.status_code in (400, 401)
    assert _login(app.test_client())["user"]["must_change_password"] is True


def test_change_requires_csrf_revokes_tokens_and_rotates_csrf(app):
    from app.extensions import db
    from app.models import LMUser
    from app.services import api_token_service

    with app.app_context():
        user = LMUser.query.filter_by(username="newbie").one()
        _row, secret = api_token_service.create(user, "old-token", 30)
        db.session.commit()
    client = app.test_client()
    data = _login(client)
    payload = {"current_password": "temporary-pass", "new_password": "replaced-pass"}
    assert client.post("/api/v1/me/password", json=payload).status_code == 403
    response = client.post("/api/v1/me/password", json=payload,
                           headers={"X-CSRF-Token": data["csrf_token"]})
    assert response.status_code == 200
    assert response.get_json()["data"]["csrf_token"] != data["csrf_token"]
    token_response = app.test_client().get("/api/v1/config",
                                          headers={"Authorization": f"Bearer {secret}"})
    assert token_response.status_code == 401


def test_authenticated_user_can_open_change_form_without_forced_flag(app):
    from app.extensions import db
    from app.models import LMUser

    with app.app_context():
        LMUser.query.filter_by(username="newbie").one().must_change_password = False
        db.session.commit()
    client = app.test_client()
    _login(client)
    response = client.get("/lanmatrix/password")
    assert response.status_code == 200
    assert 'id="lm-password-form"' in response.get_data(as_text=True)


def test_password_reauthentication_locks_persist_and_expire(app, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.extensions import db
    from app.models import LMUser
    from app.services.lanmatrix import settings

    monkeypatch.setattr(settings, "LOCK_THRESHOLD", 2)
    client = app.test_client()
    data = _login(client)
    headers = {"X-CSRF-Token": data["csrf_token"]}
    payload = {"current_password": "wrong", "new_password": "replaced-pass"}
    for failures in (1, 0):
        assert client.post("/api/v1/me/password", json=payload, headers=headers).status_code == 401
        with app.app_context():
            user = LMUser.query.filter_by(username="newbie").one()
            assert user.failed_logins == failures
    payload["current_password"] = "temporary-pass"
    assert client.post("/api/v1/me/password", json=payload, headers=headers).status_code == 423
    with app.app_context():
        user = LMUser.query.filter_by(username="newbie").one()
        assert user.must_change_password
        assert user.check_password("temporary-pass")
        user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()
    assert client.post("/api/v1/me/password", json=payload, headers=headers).status_code == 200
    with app.app_context():
        user = LMUser.query.filter_by(username="newbie").one()
        assert user.failed_logins == 0
        assert user.locked_until is None
