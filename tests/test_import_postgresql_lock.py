from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_special_import_lock_blocks_concurrent_row_writer(app_ctx):
    with app_ctx.app_context():
        from app.extensions import db
        from app.models.lanmatrix import TestItemRow
        from app.services.lanmatrix import import_job_service

        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL table-lock behavior requires PostgreSQL")

        table = db.engine.dialect.identifier_preparer.format_table(
            TestItemRow.__table__
        )
        with db.session.begin():
            import_job_service._lock_special_import_tables()
            writer = db.engine.connect()
            try:
                writer.begin()
                with pytest.raises(DBAPIError) as exc_info:
                    writer.execute(text(
                        f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE NOWAIT"
                    ))
                assert getattr(exc_info.value.orig, "pgcode", None) == "55P03"
            finally:
                writer.rollback()
                writer.close()


def test_special_io_import_commits_on_postgresql(app_ctx):
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import (
            DataJob, LMUser, Project, ProjectMember, TestItemRow,
        )
        from app.services.lanmatrix import import_job_service

        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL import integration requires PostgreSQL")

        suffix = uuid.uuid4().hex[:12]
        user = LMUser(
            username=f"pgimport{suffix}", display_name="PG Import Admin",
        )
        db.session.add(user)
        db.session.flush()
        project = Project(
            code=f"PG{suffix}", name="PostgreSQL import", owner_id=user.id,
            created_by=user.id,
        )
        db.session.add(project)
        db.session.flush()
        db.session.add(ProjectMember(
            project_id=project.id, user_id=user.id, role="project_admin",
        ))
        db.session.commit()

        job = DataJob(
            project_id=project.id,
            job_type="import",
            status="previewed",
            original_filename="io.xlsx",
            parameters={
                "mode": "upsert",
                "format": "io",
                "_snapshot": import_job_service.capture_snapshot(
                    project.id, "io",
                ),
            },
            preview={
                "mode": "upsert",
                "format": "io",
                "sheet": "io",
                "total": 1,
                "valid": 1,
                "invalid": 0,
                "rows": [{
                    "row": 2,
                    "identity": "EngSpd",
                    "values": {
                        "case_id": "EngSpd",
                        "io_name": "EngSpd",
                        "io_path": "ECU.Engine.Speed",
                        "io_note": "rpm",
                    },
                    "is_update": False,
                }],
            },
            total_count=1,
            error_count=0,
            created_by=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.session.add(job)
        db.session.commit()

        result = import_job_service.commit_special_import(user, project, job)

        row = TestItemRow.query.filter_by(
            project_id=project.id, sheet="io", case_id="EngSpd",
            deleted_at=None,
        ).one()
        assert result == {
            "inserted": 1,
            "updated": 0,
            "deleted": 0,
            "mode": "upsert",
            "format": "io",
        }
        assert row.get_field("io_path") == "ECU.Engine.Speed"
        assert db.session.get(DataJob, job.id).status == "completed"
