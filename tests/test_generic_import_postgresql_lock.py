from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def pg_import_app(app_ctx):
    from app.extensions import db

    with app_ctx.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL import locking requires PostgreSQL")
    return app_ctx


def seed_import():
    from app.extensions import db
    from app.models import DataJob, FieldDefinition, LMUser, Project, TestItemRow
    from app.services.lanmatrix import import_job_service

    suffix = uuid.uuid4().hex[:12]
    user = LMUser(username=f"generic{suffix}", display_name="Import Admin", is_system_admin=True)
    db.session.add(user)
    db.session.flush()
    project = Project(code=f"GI{suffix}", name="Generic import", owner_id=user.id)
    db.session.add(project)
    db.session.flush()
    old = TestItemRow(
        project_id=project.id, sheet="test", case_id="OLD", uuid=f"old{suffix}",
        title="original", row_order=1,
    )
    db.session.add_all([
        old,
        FieldDefinition(
            project_id=project.id, sheet="test", field_key="case_id",
            display_name="Case ID", data_type="text", display_order=1,
            is_readonly=False, is_active=True,
        ),
    ])
    db.session.commit()
    job = DataJob(
        project_id=project.id, job_type="import", status="previewed",
        parameters={
            "mode": "replace_all",
            "_snapshot": import_job_service.capture_snapshot(project.id, "test"),
        },
        preview={"invalid": 0, "rows": [{"values": {"case_id": "NEW"}}]},
        created_by=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db.session.add(job)
    db.session.commit()
    assert old.title == "original"
    return user, project, job, old


def test_generic_replace_blocks_a_writer_after_snapshot_check(pg_import_app, monkeypatch):
    with pg_import_app.app_context():
        from app.extensions import db
        from app.models import TestItemRow
        from app.services.lanmatrix import excel_service, import_job_service

        user, project, job, old = seed_import()
        project_id = project.id
        old_id = old.id
        capture = import_job_service.capture_snapshot
        probes = []

        def capture_with_writer(*args):
            snapshot = capture(*args)
            with db.engine.connect() as writer:
                transaction = writer.begin()
                try:
                    writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    with pytest.raises(DBAPIError) as error:
                        writer.execute(TestItemRow.__table__.update().where(
                            TestItemRow.id == old_id,
                        ).values(version=2, title="concurrent edit"))
                    assert getattr(error.value.orig, "pgcode", None) == "55P03"
                    probes.append("writer blocked")
                finally:
                    transaction.rollback()
            return snapshot

        monkeypatch.setattr(import_job_service, "capture_snapshot", capture_with_writer)
        result = excel_service.commit_import(user, project, job)

        assert result == {"inserted": 1, "updated": 0}
        assert probes == ["writer blocked"]
        assert db.session.get(TestItemRow, old.id).deleted_at is not None
        assert TestItemRow.query.filter_by(project_id=project_id, case_id="NEW", deleted_at=None).count() == 1
        db.session.remove()
        with db.engine.begin() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
            writer.execute(TestItemRow.__table__.insert().values(
                project_id=project_id, sheet="test", case_id="AFTER",
                uuid=uuid.uuid4().hex, row_order=99,
            ))


def test_generic_replace_reloads_cached_rows_after_acquiring_the_lock(pg_import_app, monkeypatch):
    with pg_import_app.app_context():
        from app.extensions import db
        from app.models import DataJob, TestItemRow
        from app.services.lanmatrix import excel_service, import_job_service, service

        user, project, job, old = seed_import()
        old_id, job_id, project_id = old.id, job.id, project.id
        lock_tables = import_job_service._lock_special_import_tables

        def commit_writer_then_lock():
            with db.engine.begin() as writer:
                writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                writer.execute(TestItemRow.__table__.update().where(
                    TestItemRow.id == old_id,
                ).values(version=2, title="concurrent edit"))
            lock_tables()

        monkeypatch.setattr(import_job_service, "_lock_special_import_tables", commit_writer_then_lock)

        with pytest.raises(service.ServiceError) as error:
            excel_service.commit_import(user, project, job)

        assert error.value.code == "IMPORT_PREVIEW_STALE"
        db.session.expire_all()
        current = db.session.get(TestItemRow, old_id)
        assert current.title == "concurrent edit"
        assert current.deleted_at is None
        assert db.session.get(DataJob, job_id).status == "previewed"
        assert TestItemRow.query.filter_by(project_id=project_id, case_id="NEW").count() == 0
