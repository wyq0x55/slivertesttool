"""Personal API tokens: issue with a password session, then act as the owner without one."""

import datetime as dt

import pytest


@pytest.fixture()
def app(app_ctx):
    application = app_ctx
    application.config["GLOBAL_LOGIN_REQUIRED"] = True
    with application.app_context():
        from app.extensions import db
        from app.models import LMUser, Project, ProjectMember

        user = LMUser(username="agent", display_name="Agent", password_hash="x")
        user.set_password("secret")
        db.session.add(user)
        db.session.commit()
        project = Project(code="TOK1", name="Tokens", owner_id=user.id)
        db.session.add(project)
        db.session.commit()
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role="project_admin"))
        db.session.commit()
        application._project_id = project.id
        yield application


def _issue(app, days=30):
    client = app.test_client()
    login = client.post("/api/v1/auth/login", json={"username": "agent", "password": "secret"})
    csrf = login.get_json()["data"]["csrf_token"]
    response = client.post("/api/v1/me/tokens", json={"name": "ci", "expires_days": days},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201
    data = response.get_json()["data"]
    return data["token"], data["info"]


def _bearer(secret):
    return {"Authorization": "Bearer " + secret}


def test_token_reads_and_writes_without_session_or_csrf(app):
    secret, info = _issue(app)
    assert secret.startswith("slv_") and info["prefix"] == secret[:12]
    client = app.test_client()  # fresh client: no session cookie
    pid = app._project_id
    assert client.get("/api/v1/projects", headers=_bearer(secret)).status_code == 200
    created = client.post(f"/api/v1/projects/{pid}/items", json={"sheet": "test", "values": {"case_id": "TC-1"}},
                          headers=_bearer(secret))
    assert created.status_code == 201
    listed = client.get("/api/v1/me/tokens", headers=_bearer(secret)).get_json()["data"]["tokens"]
    assert [row["name"] for row in listed] == ["ci"] and "token" not in listed[0]
    assert listed[0]["last_used_at"] is not None


def test_secret_is_stored_only_as_a_digest(app):
    from app.models import ApiToken

    secret, _ = _issue(app)
    stored = ApiToken.query.one()
    assert stored.token_hash != secret and secret not in (stored.prefix, stored.token_hash)


@pytest.mark.parametrize("header", ["Bearer slv_wrong", "Bearer ", "Basic abc"])
def test_invalid_tokens_are_unauthenticated(app, header):
    response = app.test_client().get("/api/v1/projects", headers={"Authorization": header})
    assert response.status_code == 401


def test_invalid_token_is_not_rescued_by_a_session_cookie(app):
    client = app.test_client()
    client.post("/api/v1/auth/login", json={"username": "agent", "password": "secret"})
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/projects", headers=_bearer("slv_wrong")).status_code == 401


def test_revoked_and_expired_tokens_stop_working(app):
    from app.extensions import db
    from app.models import ApiToken

    secret, info = _issue(app)
    other, _ = _issue(app)
    client = app.test_client()
    assert client.delete(f"/api/v1/me/tokens/{info['id']}", headers=_bearer(other)).status_code == 200
    assert client.get("/api/v1/projects", headers=_bearer(secret)).status_code == 401
    token = ApiToken.query.filter_by(revoked_at=None).one()
    token.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.session.commit()
    assert client.get("/api/v1/projects", headers=_bearer(other)).status_code == 401


def test_disabled_owner_disables_the_token(app):
    from app.extensions import db
    from app.models import LMUser

    secret, _ = _issue(app)
    LMUser.query.filter_by(username="agent").one().status = "disabled"
    db.session.commit()
    assert app.test_client().get("/api/v1/projects", headers=_bearer(secret)).status_code == 401


def test_token_cannot_mint_tokens(app):
    secret, _ = _issue(app)
    response = app.test_client().post("/api/v1/me/tokens", json={"name": "x", "expires_days": 1},
                                      headers=_bearer(secret))
    assert response.status_code == 403


@pytest.mark.parametrize("days", [0, 366, "30", None])
def test_token_lifetime_is_bounded(app, days):
    client = app.test_client()
    csrf = client.post("/api/v1/auth/login", json={"username": "agent", "password": "secret"}).get_json()["data"]["csrf_token"]
    response = client.post("/api/v1/me/tokens", json={"name": "x", "expires_days": days}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400


def test_replace_rotates_tokens_of_the_same_name(app):
    first, _ = _issue(app)
    client = app.test_client()
    csrf = client.post("/api/v1/auth/login", json={"username": "agent", "password": "secret"}).get_json()["data"]["csrf_token"]
    response = client.post("/api/v1/me/tokens", json={"name": "ci", "expires_days": 30, "replace": True},
                           headers={"X-CSRF-Token": csrf})
    second = response.get_json()["data"]["token"]
    fresh = app.test_client()
    assert fresh.get("/api/v1/projects", headers=_bearer(first)).status_code == 401
    assert fresh.get("/api/v1/projects", headers=_bearer(second)).status_code == 200


def test_token_page_needs_login_and_renders(app):
    client = app.test_client()
    assert client.get("/lanmatrix/tokens").status_code == 302
    client.post("/api/v1/auth/login", json={"username": "agent", "password": "secret"})
    page = client.get("/lanmatrix/tokens")
    assert page.status_code == 200 and "lm-token-form" in page.get_data(as_text=True)


def _make_admin(app):
    from app.extensions import db
    from app.models import LMUser

    LMUser.query.filter_by(username="agent").one().is_system_admin = True
    db.session.commit()


def test_token_cannot_manage_accounts_even_as_admin(app):
    """A leaked admin token must not create users or reset passwords, then log in to mint tokens."""
    from app.models import LMUser

    _make_admin(app)
    secret, _ = _issue(app)
    client = app.test_client()
    other = LMUser.query.filter_by(username="admin").one()
    created = client.post("/api/v1/admin/users", headers=_bearer(secret),
                          json={"username": "mallory", "password": "mallory-pass", "is_system_admin": True})
    assert created.status_code == 403
    assert LMUser.query.filter_by(username="mallory").first() is None
    assert client.patch(f"/api/v1/admin/users/{other.id}", headers=_bearer(secret),
                        json={"password": "hijacked-pass"}).status_code == 403
    assert client.delete(f"/api/v1/admin/users/{other.id}", headers=_bearer(secret)).status_code == 403
    assert client.get("/api/v1/admin/users", headers=_bearer(secret)).status_code == 200


def test_password_reset_revokes_the_users_tokens(app):
    from app.models import LMUser

    secret, _ = _issue(app)
    agent = LMUser.query.filter_by(username="agent").one()
    admin = LMUser.query.filter_by(username="admin").one()
    admin.set_password("admin-pass")
    admin.must_change_password = False
    from app.extensions import db
    db.session.commit()
    client = app.test_client()
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-pass"})
    csrf = login.get_json()["data"]["csrf_token"]
    reset = client.patch(f"/api/v1/admin/users/{agent.id}", json={"password": "brand-new-pass"},
                         headers={"X-CSRF-Token": csrf})
    assert reset.status_code == 200
    assert app.test_client().get("/api/v1/projects", headers=_bearer(secret)).status_code == 401


def _rollback_read(reader):
    from app.extensions import db

    db.session.rollback()
    return reader()


def test_admin_token_cannot_change_license_or_runtime_config(app, monkeypatch):
    """An admin API token may read license and runtime config, but not write them."""
    from app.routes.lanmatrix import admin_console
    from app.services import license_service, runtime_config

    _make_admin(app)
    secret, _ = _issue(app)
    calls = []

    def _forbid(name):
        def wrapped(*args, **kwargs):
            calls.append(name)
            raise AssertionError(name + " mutated")
        return wrapped

    monkeypatch.setattr(admin_console.license_service, "set_limit", _forbid("license"))
    monkeypatch.setattr(admin_console.runtime_config, "set_many", _forbid("runtime"))
    limit_before = _rollback_read(license_service.get_limit)
    timeout_before = _rollback_read(lambda: runtime_config.get_int("execution_timeout"))
    client = app.test_client()
    headers = _bearer(secret)
    assert client.get("/api/v1/admin/license", headers=headers).status_code == 200
    assert client.get("/api/v1/admin/runtime-config", headers=headers).status_code == 200
    licensed = client.post("/api/v1/admin/license", headers=headers,
                           json={"count": limit_before + 1})
    configured = client.post("/api/v1/admin/runtime-config", headers=headers,
                             json={"changes": {"execution_timeout": timeout_before + 1}})
    assert licensed.status_code == 403
    assert configured.status_code == 403
    assert licensed.get_json()["error"]["code"] == "PERMISSION_DENIED"
    assert configured.get_json()["error"]["code"] == "PERMISSION_DENIED"
    assert "API Token" in licensed.get_json()["error"]["message"]
    assert "API Token" in configured.get_json()["error"]["message"]
    assert calls == []
    assert _rollback_read(license_service.get_limit) == limit_before
    assert _rollback_read(lambda: runtime_config.get_int("execution_timeout")) == timeout_before


def test_password_admin_session_can_change_license_and_runtime_config(app):
    from app.services import license_service, runtime_config

    _make_admin(app)
    limit_before = _rollback_read(license_service.get_limit)
    timeout_before = _rollback_read(lambda: runtime_config.get_int("execution_timeout"))
    next_limit = limit_before + 1
    next_timeout = timeout_before + 1
    client = app.test_client()
    login = client.post("/api/v1/auth/login", json={"username": "agent", "password": "secret"})
    csrf = login.get_json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    licensed = client.post("/api/v1/admin/license", headers=headers, json={"count": next_limit})
    configured = client.post("/api/v1/admin/runtime-config", headers=headers,
                             json={"changes": {"execution_timeout": next_timeout}})
    assert licensed.status_code == 200
    assert licensed.get_json()["data"]["count"] == next_limit
    assert configured.status_code == 200
    assert configured.get_json()["data"]["values"]["execution_timeout"] == next_timeout
    assert _rollback_read(license_service.get_limit) == next_limit
    assert _rollback_read(lambda: runtime_config.get_int("execution_timeout")) == next_timeout


def test_non_admin_still_cannot_change_license_or_runtime_config(app, monkeypatch):
    """Non-admins stay on the existing system-admin denial, with no write."""
    from app.routes.lanmatrix import admin_console
    from app.services import license_service, runtime_config

    calls = []

    def _forbid(name):
        def wrapped(*args, **kwargs):
            calls.append(name)
            raise AssertionError(name + " mutated")
        return wrapped

    monkeypatch.setattr(admin_console.license_service, "set_limit", _forbid("license"))
    monkeypatch.setattr(admin_console.runtime_config, "set_many", _forbid("runtime"))
    secret, _ = _issue(app)
    limit_before = _rollback_read(license_service.get_limit)
    timeout_before = _rollback_read(lambda: runtime_config.get_int("execution_timeout"))
    payload_license = {"count": limit_before + 1}
    payload_runtime = {"changes": {"execution_timeout": timeout_before + 1}}
    token_client = app.test_client()
    token_headers = _bearer(secret)
    token_license = token_client.post("/api/v1/admin/license", headers=token_headers, json=payload_license)
    token_runtime = token_client.post("/api/v1/admin/runtime-config", headers=token_headers, json=payload_runtime)
    session_client = app.test_client()
    login = session_client.post("/api/v1/auth/login", json={"username": "agent", "password": "secret"})
    csrf = login.get_json()["data"]["csrf_token"]
    session_headers = {"X-CSRF-Token": csrf}
    session_license = session_client.post("/api/v1/admin/license", headers=session_headers, json=payload_license)
    session_runtime = session_client.post("/api/v1/admin/runtime-config", headers=session_headers, json=payload_runtime)
    for response in (token_license, token_runtime, session_license, session_runtime):
        body = response.get_json()
        assert response.status_code == 403
        assert body["error"]["code"] == "PERMISSION_DENIED"
        assert body["error"]["message"] == "仅系统管理员可访问"
    assert calls == []
    assert _rollback_read(license_service.get_limit) == limit_before
    assert _rollback_read(lambda: runtime_config.get_int("execution_timeout")) == timeout_before
