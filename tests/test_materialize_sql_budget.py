"""SQL statement-budget regression tests for CRDT materialization & PgYStore."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event


@pytest.fixture()
def isolated_app(tmp_path):
    """Isolated SQLite app solely for counting ORM/SAVEPOINT SQL round-trips."""
    import app as app_pkg
    from app.config import Config
    from app.extensions import db
    import app.models  # noqa: F401

    class SqlBudgetConfig(Config):
        TESTING = True
        SECRET_KEY = "sql-budget-test"
        GLOBAL_LOGIN_REQUIRED = False
        INSTANCE_DIR = Path(tmp_path) / "instance"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{(tmp_path / 'budget.db').as_posix()}"
        SQLALCHEMY_ENGINE_OPTIONS = {}

    application = app_pkg.create_app(SqlBudgetConfig)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.engine.dispose()


def test_materialize_sheet_skips_savepoints_for_unchanged_rows(isolated_app):
    from app.extensions import db
    from app.models import LMUser, Project
    from app.services.lanmatrix import items_service

    with isolated_app.app_context():
        user = LMUser(username="mat_perf", display_name="Perf")
        db.session.add(user)
        db.session.flush()
        project = Project(code="MATSQL", name="Mat SQL", status="active",
                          owner_id=user.id, created_by=user.id)
        db.session.add(project)
        db.session.commit()

        rows = [
            {"uuid": f"{i:032x}", "title": f"Case {i}", "custom_col": f"val-{i}"}
            for i in range(500)
        ]
        initial = items_service.materialize_sheet(user, project, "test", rows)
        assert initial["created"] == 500

        edited = [dict(r) for r in rows]
        edited[250]["title"] = "Updated Single Cell"

        statements: list[str] = []

        def _record(_conn, _cursor, statement, _params, _ctx, _many):
            statements.append(statement.strip().upper())

        event.listen(db.engine, "before_cursor_execute", _record)
        try:
            summary = items_service.materialize_sheet(user, project, "test", edited)
        finally:
            event.remove(db.engine, "before_cursor_execute", _record)

        assert summary["updated"] == 1
        assert summary["unchanged"] == 499
        savepoints = [s for s in statements if s.startswith("SAVEPOINT")]
        assert len(savepoints) == 1, f"Expected 1 SAVEPOINT for 1 dirty row, got {len(savepoints)}"


def test_pg_ystore_compaction_uses_single_bulk_delete(isolated_app):
    pycrdt = pytest.importorskip("pycrdt")
    from app.collab import pg_ystore
    from app.collab.pg_ystore import PgYStore
    from app.extensions import db
    from app.models import LMUser, Project

    with isolated_app.app_context():
        user = LMUser(username="ystore_perf", display_name="Y")
        db.session.add(user)
        db.session.flush()
        project = Project(code="YSQL", name="YSQL", status="active",
                          owner_id=user.id, created_by=user.id)
        db.session.add(project)
        db.session.commit()
        pid = project.id

    store = PgYStore(f"project:{pid}", isolated_app)
    doc = pycrdt.Doc()
    doc["m"] = m = pycrdt.Map()
    for i in range(10):
        before = doc.get_state()
        with doc.transaction():
            m[f"k{i}"] = i
        store._write_sync(doc.get_update(before), None, 1.0 + i)

    deletes: list[str] = []

    def _record(_conn, _cursor, statement, _params, _ctx, _many):
        sql = statement.strip().upper()
        if sql.startswith("DELETE"):
            deletes.append(sql)

    with isolated_app.app_context():
        event.listen(db.engine, "before_cursor_execute", _record)
        try:
            store._compact_locked()
        finally:
            event.remove(db.engine, "before_cursor_execute", _record)
        rows = store._read_sync()

    assert len(deletes) == 1, f"Expected 1 bulk DELETE, got {len(deletes)}"
    assert len(rows) == 1
    rebuilt = pycrdt.Doc()
    rebuilt["m"] = rm = pycrdt.Map()
    rebuilt.apply_update(rows[0][0])
    assert dict(rm) == {f"k{i}": i for i in range(10)}
