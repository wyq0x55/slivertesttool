"""pytest fixtures: a Flask app + client backed by a PostgreSQL test database.

The platform runs exclusively on PostgreSQL, so the tests need a reachable
PostgreSQL instance. Point them at it with ``TEST_DATABASE_URL`` (falling back
to ``DATABASE_URL``); if neither is set a local default DSN is used. The target
database is wiped (``drop_all`` + ``create_all``) at the start of each test for
isolation, so use a dedicated throwaway database.

Huey runs in immediate mode so enqueued tasks execute synchronously in-process
(via an in-memory queue), letting the API tests exercise the full
upload -> queue -> run -> result flow without a persistent queue.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg2://postgres:postgres@localhost:5432/silvetestapp_test"
)


@pytest.fixture()
def app_ctx(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="stp_test_")
    root = Path(tmp)
    test_db_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_TEST_DATABASE_URL
    )
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")
    monkeypatch.setenv("RUNNER_BACKEND", "mock")
    monkeypatch.setenv("INSTANCE_DIR", str(root / "instance"))
    monkeypatch.setenv("UPLOAD_DIR", str(root / "uploads"))
    monkeypatch.setenv("REPORT_DIR", str(root / "reports"))
    monkeypatch.setenv("WORKSPACE_DIR", str(root / "ws"))
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("LICENSE_LIMIT", "2")
    # The legacy API tests exercise the original endpoints directly; keep the
    # unified-login gate off for them (it is covered separately).
    monkeypatch.setenv("GLOBAL_LOGIN_REQUIRED", "0")

    # Import lazily so the env vars above are picked up by Config.
    import importlib

    import app as app_pkg
    import app.config as config_mod

    importlib.reload(config_mod)
    importlib.reload(app_pkg)

    # Reset the (shared) PostgreSQL test database for isolation between tests:
    # wipe any schema left by a previous run, then let ``create_app`` recreate
    # the tables and seed the license/admin defaults into an empty database.
    from app.extensions import db

    reset_app = app_pkg.create_app(config_mod.Config)
    with reset_app.app_context():
        db.drop_all()

    application = app_pkg.create_app(config_mod.Config)
    yield application


@pytest.fixture()
def client(app_ctx):
    return app_ctx.test_client()
