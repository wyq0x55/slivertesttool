from flask import abort


def test_api_routing_errors_are_json_and_preserve_headers(app_ctx):
    client = app_ctx.test_client()
    missing = client.get("/api/v1/does-not-exist")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "NOT_FOUND"
    wrong_method = client.delete("/api/v1/auth/login")
    assert wrong_method.status_code == 405
    assert wrong_method.get_json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "POST" in wrong_method.headers["Allow"]
    assert client.get("/does-not-exist").mimetype == "text/html"


def test_unexpected_errors_rollback_without_exposing_exception(app_ctx):
    from app.extensions import db
    from app.models import Setting

    @app_ctx.get("/api/v1/_test-error")
    def broken():
        db.session.add(Setting(key="error_rollback", value="pending"))
        db.session.flush()
        raise RuntimeError("private-exception-detail")

    response = app_ctx.test_client().get("/api/v1/_test-error")
    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private-exception-detail" not in response.get_data(as_text=True)
    with app_ctx.app_context():
        assert db.session.get(Setting, "error_rollback") is None


def test_explicit_http_error_stays_a_client_error(app_ctx):
    @app_ctx.get("/api/v1/_test-invalid")
    def invalid():
        abort(422, description="invalid input")

    response = app_ctx.test_client().get("/api/v1/_test-invalid")
    assert response.status_code == 422
    assert response.get_json()["error"]["message"] == "invalid input"
