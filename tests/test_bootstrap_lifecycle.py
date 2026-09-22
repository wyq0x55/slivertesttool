"""Bootstrap ownership contract tests."""

from __future__ import annotations

from pathlib import Path


def test_create_app_does_not_bootstrap(monkeypatch, tmp_path):
    import app as app_pkg
    from app.config import Config
    from app.extensions import db

    class FactoryOnlyConfig(Config):
        TESTING = True
        SECRET_KEY = "factory-only-test"
        GLOBAL_LOGIN_REQUIRED = False
        INSTANCE_DIR = Path(tmp_path) / "instance"
        SQLALCHEMY_DATABASE_URI = (
            "postgresql+psycopg2://invalid:invalid@127.0.0.1:1/never-connect"
        )

        @classmethod
        def ensure_dirs(cls):
            raise AssertionError("create_app() must not create persistent directories")

    def fail_create_all(*_args, **_kwargs):
        raise AssertionError("create_app() must not create database schema")

    monkeypatch.setattr(db, "create_all", fail_create_all)

    instance_dir = FactoryOnlyConfig.INSTANCE_DIR
    assert not instance_dir.exists()

    application = app_pkg.create_app(FactoryOnlyConfig)

    assert not instance_dir.exists()
    assert application.config_obj is FactoryOnlyConfig
    assert application.config["TESTING"] is True


def test_bootstrap_app_is_idempotent(app_ctx):
    from app.bootstrap import bootstrap_app
    from app.models import LMUser

    bootstrap_app(app_ctx)
    bootstrap_app(app_ctx)

    with app_ctx.app_context():
        assert LMUser.query.filter_by(is_system_admin=True).count() == 1


def test_immediate_huey_schema_bootstrap_is_noop(monkeypatch):
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")

    import importlib
    import app.jobqueue.huey_app as huey_app

    importlib.reload(huey_app)
    huey_app.ensure_huey_schema()
