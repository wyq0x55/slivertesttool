"""Database-backed tests for the run write-back loop.

The sibling tests/test_run_writeback.py covers the pure helpers. Those passed
while the loop was in fact broken: ``_matching_rows`` narrowed candidates with a
JSONB-only operator, which raises on every other dialect, and ``record_run``
swallows exceptions by design (a finished run must not fail because its evidence
could not be filed). The result was a silent no-op -- nothing written, nothing
raised. These tests therefore drive ``record_run`` against a real database and
assert on the row and the run record afterwards.

Builds its own SQLite app for the same reason as tests/test_dashboard.py: the
shared conftest fixture requires PostgreSQL.
"""
from __future__ import annotations

import importlib
import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture(scope="module")
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old_url = os.environ.get("DATABASE_URL")
    old_secret = os.environ.get("SECRET_KEY")
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["SECRET_KEY"] = "writeback-test"

    import app.config as config_mod
    import app as app_pkg
    importlib.reload(config_mod)
    importlib.reload(app_pkg)

    application = app_pkg.create_app()
    with application.app_context():
        yield application

    if old_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old_url
    if old_secret is None:
        os.environ.pop("SECRET_KEY", None)
    else:
        os.environ["SECRET_KEY"] = old_secret
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def seeded(app):
    """A project with one versioned model, one row and one finished task."""
    from app.extensions import db
    from app.models import (LMUser, Project, ProjectMember, ProjectModel, Task,
                            TestItemRow, TestRunRecord)

    suffix = datetime.utcnow().strftime("%H%M%S%f")
    user = LMUser(username=f"wb{suffix}", display_name="実施者A",
                  password_hash="x")
    db.session.add(user)
    db.session.commit()

    project = Project(code=f"WB{suffix}", name="Writeback", owner_id=user.id)
    db.session.add(project)
    db.session.commit()
    db.session.add(ProjectMember(project_id=project.id, user_id=user.id,
                                 role="project_admin"))
    db.session.add(ProjectModel(project_id=project.id, name="engine",
                                sil_path="/srv/engine.sil", kind="path",
                                is_current=True, version="v2.3.1"))
    db.session.add(TestItemRow(project_id=project.id, sheet="test",
                               uuid=f"row-{suffix}", case_id="TC-001",
                               title="idle", result="Not Tested"))
    db.session.commit()

    task = Task(project_id=project.id, test_id="TC-001",
                task_key=f"job{suffix}"[:16], status="finished",
                submitter_id=user.id, sil_name="engine",
                finished_at=datetime(2026, 8, 10, 1, 30, tzinfo=timezone.utc))
    db.session.add(task)
    db.session.commit()
    return project, user, task


class TestRecordRun:
    def test_stamps_every_evidence_column_on_the_row(self, app, seeded):
        """The whole point of the loop: verdict *and* who/which version/when."""
        from app.extensions import db
        from app.models import TestItemRow
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, task = seeded
        assert rws.record_run(task, "PASS") == 1

        db.session.expire_all()
        row = TestItemRow.query.filter_by(project_id=project.id).first()
        assert (row.result or "").upper() == "PASS"
        values = row.custom_values or {}
        assert values.get("version_label") == "v2.3.1"
        assert values.get("executor") == "実施者A"
        # LM_DISPLAY_TZ is Beijing, so 01:30Z stays on the 10th.
        assert values.get("exec_date") == "2026-08-10"

    def test_appends_an_immutable_run_record(self, app, seeded):
        from app.models import TestRunRecord
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, task = seeded
        rws.record_run(task, "PASS")

        records = TestRunRecord.query.filter_by(project_id=project.id).all()
        assert len(records) == 1
        assert records[0].model_version == "v2.3.1"
        assert (records[0].outcome or "").lower() == "pass"
        assert records[0].executor_name == "実施者A"

    def test_a_rerun_appends_history_and_updates_the_row(self, app, seeded):
        """The row shows the latest verdict; history keeps the earlier one.

        The dashboard's burn-up needs the runs before the last one, so a re-run
        must not overwrite its own history.
        """
        from app.extensions import db
        from app.models import Task, TestItemRow, TestRunRecord
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, task = seeded
        rws.record_run(task, "PASS")

        again = Task(project_id=project.id, test_id="TC-001",
                     task_key=(task.task_key + "b")[:16], status="finished",
                     submitter_id=user.id, sil_name="engine",
                     finished_at=datetime(2026, 8, 11, 2, 0,
                                          tzinfo=timezone.utc))
        db.session.add(again)
        db.session.commit()
        rws.record_run(again, "FAIL")

        records = TestRunRecord.query.filter_by(project_id=project.id).all()
        assert len(records) == 2
        db.session.expire_all()
        row = TestItemRow.query.filter_by(project_id=project.id).first()
        assert (row.result or "").upper() == "FAIL"

    def test_matches_a_row_by_its_test_id_field(self, app, seeded):
        """The logical id may live in custom_values rather than case_id.

        This is the path that used to need a JSONB operator; it has to work on
        whatever database the deployment actually runs.
        """
        from app.extensions import db
        from app.models import Task, TestItemRow
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, _task = seeded
        row = TestItemRow(project_id=project.id, sheet="test",
                          uuid="row-json", case_id="",
                          title="json id", result="Not Tested",
                          custom_values={"test_id": "TC-JSON-9"})
        db.session.add(row)
        db.session.commit()

        task = Task(project_id=project.id, test_id="TC-JSON-9",
                    task_key="jsonjob", status="finished",
                    submitter_id=user.id, sil_name="engine",
                    finished_at=datetime(2026, 8, 10, 1, 30,
                                         tzinfo=timezone.utc))
        db.session.add(task)
        db.session.commit()

        assert rws.record_run(task, "PASS") == 1
        db.session.expire_all()
        found = TestItemRow.query.filter_by(project_id=project.id,
                                            uuid="row-json").first()
        assert (found.result or "").upper() == "PASS"
        assert (found.custom_values or {}).get("executor") == "実施者A"

    def test_an_unrelated_row_is_left_alone(self, app, seeded):
        """A LIKE prefilter matches loosely, so the exact rule must still hold."""
        from app.extensions import db
        from app.models import TestItemRow
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, task = seeded
        other = TestItemRow(project_id=project.id, sheet="test",
                            uuid="row-other", case_id="TC-0011",
                            title="looks similar", result="Not Tested")
        db.session.add(other)
        db.session.commit()

        rws.record_run(task, "PASS")
        db.session.expire_all()
        untouched = TestItemRow.query.filter_by(project_id=project.id,
                                                uuid="row-other").first()
        assert untouched.result == "Not Tested"
        assert not (untouched.custom_values or {}).get("executor")

    def test_no_matching_row_is_not_an_error(self, app, seeded):
        from app.extensions import db
        from app.models import Task
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, _ = seeded
        orphan = Task(project_id=project.id, test_id="TC-NOPE",
                      task_key="orphan", status="finished",
                      submitter_id=user.id, sil_name="engine",
                      finished_at=datetime(2026, 8, 10, 1, 30,
                                           tzinfo=timezone.utc))
        db.session.add(orphan)
        db.session.commit()
        assert rws.record_run(orphan, "PASS") == 0


class TestDialectPortability:
    """Compile the write-back query for PostgreSQL without a PostgreSQL server.

    This is the class of bug that shipped: ``custom_values`` is declared
    ``JSON().with_variant(JSONB, "postgresql")``, which reads as "JSONB on
    PostgreSQL" and invites JSONB-only accessors like ``.astext``. But
    ``with_variant`` only substitutes the type at *compile* time; the
    Python-side comparator is always built from the base ``JSON`` type, so
    ``.astext`` raises on every dialect -- and ``record_run`` swallows the
    exception, so nothing was written and nothing was raised.

    A SQLite-only suite cannot see a PostgreSQL-only break, so instead of
    running the query we compile it against each dialect. That needs no server
    and still fails loudly if someone reaches for a dialect-locked operator.
    """

    def _predicate(self):
        from app.models import TestItemRow
        return TestItemRow.custom_values["test_id"].as_string() == "TC-1"

    @pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite", "mysql"])
    def test_the_json_predicate_compiles_everywhere(self, app, dialect_name):
        import importlib
        dialects = importlib.import_module(
            f"sqlalchemy.dialects.{dialect_name}")
        compiled = str(self._predicate().compile(dialect=dialects.dialect()))
        assert compiled  # compiling at all is the assertion that matters

    def test_postgres_uses_the_native_json_accessor(self, app):
        from sqlalchemy.dialects import postgresql
        compiled = str(self._predicate().compile(dialect=postgresql.dialect()))
        # ->> keeps this an indexable predicate rather than a full-table scan.
        assert "->>" in compiled

    def test_record_run_reports_failure_instead_of_pretending(self, app,
                                                              seeded,
                                                              monkeypatch):
        """A broken query must not look like 'no rows matched'.

        record_run is deliberately best-effort, which is what hid the original
        bug. Best-effort is still the right call -- a finished run must not fail
        because its evidence could not be filed -- but the failure has to be
        distinguishable from a clean no-op, so operators can find it.
        """
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, task = seeded

        def boom(*_a, **_kw):
            raise AttributeError("simulated dialect-only operator")

        monkeypatch.setattr(rws, "_matching_rows", boom)
        # Must not propagate: the run already finished.
        assert rws.record_run(task, "PASS") == 0


class TestCollabDelivery:
    """Drive the queue -> Y.Doc hop that the other tests never execute.

    ``record_run`` writing the database is only half the loop. When somebody has
    the sheet open, the ``Y.Doc`` -- not the database -- is the authoritative
    copy of a row, so the values also have to travel
    ``RowWriteback -> claim_pending -> write_row_fields -> Y.Doc``. Every earlier
    test stopped at the database, which is exactly why a break anywhere in this
    hop would have looked like "the write-back silently did nothing".

    The document is bootstrapped *before* the run on purpose: bootstrapping
    afterwards makes the doc read the fresh values straight from the database,
    so the assertion would pass even with the drain removed entirely.
    """

    SERVER_FIELDS = ("exec_date", "executor", "version_label")

    def _activate_collab(self, project_id):
        from app.extensions import db
        from app.models import CollabPresence

        db.session.add(CollabPresence(project_id=project_id, connections=1,
                                      updated_at=datetime.utcnow()))
        db.session.commit()

    def test_values_reach_an_already_open_document(self, app, seeded):
        pytest.importorskip("pycrdt")
        from pycrdt import Doc

        from app.collab import doc_model, writeback
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, task = seeded
        self._activate_collab(project.id)

        # The editor is open on the stale row before the run finishes.
        doc = Doc()
        assert doc_model.bootstrap_doc(doc, project.id) == 1
        before = doc_model.snapshot_sheet(doc, "test")[0]
        assert before.get("result") == "Not Tested"
        assert not before.get("executor")

        assert rws.record_run(task, "PASS") == 1

        claimed = writeback.claim_pending([project.id])
        assert project.id in claimed, "run was not queued for the live room"
        with doc.transaction():
            changed = doc_model.write_row_fields(doc, "test",
                                                 claimed[project.id])
        assert changed == 1

        row = doc_model.snapshot_sheet(doc, "test")[0]
        assert row.get("result") == "PASS"
        for key in self.SERVER_FIELDS:
            assert row.get(key), f"{key} never reached the open document"

    def test_nothing_is_queued_when_no_one_is_editing(self, app, seeded):
        """Without a live room the database alone is authoritative.

        Queueing anyway would leave rows no collab server ever drains, to be
        replayed later onto a much newer document.
        """
        from app.models import RowWriteback
        from app.services.lanmatrix import run_writeback_service as rws

        project, user, task = seeded
        assert rws.record_run(task, "PASS") == 1
        assert RowWriteback.query.filter_by(project_id=project.id).count() == 0
