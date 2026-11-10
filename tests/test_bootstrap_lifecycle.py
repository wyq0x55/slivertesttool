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


def test_migrate_data_job_status_check(app_ctx):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.bootstrap import reconcile_schema
    from app.extensions import db
    from app.models import DataJob, Project

    with app_ctx.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL check constraint migration requires PostgreSQL")

        # Simulate legacy constraint
        with db.engine.begin() as conn:
            conn.execute(text("""
                ALTER TABLE lm_data_jobs
                DROP CONSTRAINT IF EXISTS lm_data_jobs_status_check;

                ALTER TABLE lm_data_jobs
                ADD CONSTRAINT lm_data_jobs_status_check
                CHECK (status IN ('pending', 'running', 'success', 'failed', 'cancelled'));
            """))

        # Reconcile schema should upgrade the constraint
        reconcile_schema()

        with db.engine.connect() as conn:
            definition = conn.execute(text("""
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'lm_data_jobs'::regclass
                  AND conname = 'lm_data_jobs_status_check'
            """)).scalar()

        assert definition is not None
        assert "'previewed'" in definition
        assert "'completed'" in definition

        project = Project.query.first()
        if not project:
            project = Project(code="PTEST_MIGRATE", name="Migrate Test")
            db.session.add(project)
            db.session.commit()

        job = DataJob(
            project_id=project.id,
            job_type="import",
            status="previewed",
        )
        db.session.add(job)
        db.session.commit()

        job.status = "completed"
        db.session.commit()

        db.session.delete(job)
        db.session.commit()

        # Invalid status should still be rejected by the constraint
        bad_job = DataJob(
            project_id=project.id,
            job_type="import",
            status="invalid_status",
        )
        db.session.add(bad_job)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

