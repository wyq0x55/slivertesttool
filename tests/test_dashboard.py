"""Tests for the project dashboard aggregates.

These lock down the counting rules that make the dashboard trustworthy:
the scope/progress splits must always close, re-runs must not inflate
progress, and cancelled runs must not be counted as work done.

Like tests/test_notifications.py this builds its own SQLite app, because the
shared conftest fixture requires PostgreSQL. The reload dance is required so
the DATABASE_URL override is picked up by the already-imported config module.
"""
from __future__ import annotations

import importlib
import os
import tempfile
from datetime import datetime, timedelta

import pytest


@pytest.fixture(scope="module")
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old_url = os.environ.get("DATABASE_URL")
    old_secret = os.environ.get("SECRET_KEY")
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["SECRET_KEY"] = "dashboard-test"

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
    """A project with a known mix of rows; returns (project, user)."""
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, TestItemRow
    from app.models import TestRunRecord

    suffix = datetime.utcnow().strftime("%H%M%S%f")
    user = LMUser(username=f"dash{suffix}", display_name="Dash",
                  password_hash="x")
    db.session.add(user)
    db.session.commit()

    project = Project(code=f"D{suffix}"[:16], name="Dash", owner_id=user.id)
    db.session.add(project)
    db.session.commit()
    db.session.add(ProjectMember(project_id=project.id, user_id=user.id,
                                 role="project_admin"))
    db.session.commit()

    plan = ([("PASS", "")] * 8 + [("FAIL", "")] * 3 + [("ERROR", "")] * 2
            + [("Untestable", "")] * 2 + [("", "")] * 3
            + [("", "Archived")] * 2)
    for i, (result, wf) in enumerate(plan):
        db.session.add(TestItemRow(
            project_id=project.id, sheet="test", uuid=f"{suffix}-row-{i}",
            case_id=f"TC-{i:03d}", title=f"case {i}",
            result=result or "Not Tested",
            workflow_status=wf or "Draft"))
    db.session.commit()

    yield project, user

    TestRunRecord.query.filter_by(project_id=project.id).delete()
    TestItemRow.query.filter_by(project_id=project.id).delete()
    ProjectMember.query.filter_by(project_id=project.id).delete()
    db.session.delete(project)
    db.session.commit()


def _add_runs(project, user, rows):
    """rows: list of (executed_on, version, row_uuid, outcome, hour_offset)."""
    from app.extensions import db
    from app.models import TestRunRecord

    base = datetime(2026, 3, 1, 9, 0, 0)
    for on, ver, uuid, outcome, hrs in rows:
        db.session.add(TestRunRecord(
            project_id=project.id, row_uuid=f"{uuid}", test_id=uuid,
            task_key="T", verdict=outcome.upper(), outcome=outcome,
            model_name="m", model_version=ver,
            executor_id=user.id, executor_name="Dash",
            executed_at=base + timedelta(hours=hrs), executed_on=on))
    db.session.commit()


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------
class TestSummary:
    def test_counts_match_the_seeded_mix(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        s = ds.summary(project.id)
        assert s["total"] == 20
        assert s["out_of_scope"] == 2
        assert s["passed"] == 8
        assert s["failed"] == 3
        assert s["errored"] == 2
        assert s["untestable"] == 2
        assert s["not_run"] == 3

    def test_scope_split_closes(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        s = ds.summary(project.id)
        assert s["out_of_scope"] + s["planned"] == s["total"]

    def test_progress_split_closes(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        s = ds.summary(project.id)
        assert s["not_run"] + s["executed"] == s["planned"]

    def test_executed_is_the_sum_of_its_outcomes(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        s = ds.summary(project.id)
        assert (s["passed"] + s["failed"] + s["errored"] + s["untestable"]
                == s["executed"])

    def test_percentages_are_relative_to_planned_not_total(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        s = ds.summary(project.id)
        # 15/18, not 15/20 -- archived rows are out of scope, so counting
        # them in the denominator would understate real progress.
        assert s["executed_pct"] == pytest.approx(83.3, abs=0.1)

    def test_deleted_rows_are_excluded(self, seeded):
        from app.extensions import db
        from app.models import TestItemRow
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        row = TestItemRow.query.filter_by(project_id=project.id,
                                          result="PASS").first()
        row.deleted_at = datetime.utcnow()
        db.session.commit()
        s = ds.summary(project.id)
        assert s["total"] == 19
        assert s["passed"] == 7

    def test_non_test_sheets_are_excluded(self, seeded):
        from app.extensions import db
        from app.models import TestItemRow
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        db.session.add(TestItemRow(
            project_id=project.id, sheet="summary", uuid="other-sheet",
            case_id="X", title="not a test row", result="PASS"))
        db.session.commit()
        s = ds.summary(project.id)
        assert s["total"] == 20, "only the test sheet feeds the dashboard"

    def test_empty_project_is_all_zeros_not_a_crash(self, app):
        from app.extensions import db
        from app.models import LMUser, Project
        from app.services.lanmatrix import dashboard_service as ds
        user = LMUser(username="empty-owner", display_name="E",
                      password_hash="x")
        db.session.add(user)
        db.session.commit()
        project = Project(code="EMPTY", name="Empty", owner_id=user.id)
        db.session.add(project)
        db.session.commit()
        s = ds.summary(project.id)
        assert s["total"] == 0
        assert s["executed_pct"] == 0
        assert s["passed_pct"] == 0


# --------------------------------------------------------------------------
# trend
# --------------------------------------------------------------------------
class TestTrend:
    def test_counts_first_execution_per_case(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, user = seeded
        uuid_of = lambda i: TestTrendHelper.uuid(project, i)
        _add_runs(project, user, [
            ("2026-03-01", "1.0", uuid_of(0), "pass", 0),
            ("2026-03-01", "1.0", uuid_of(1), "fail", 1),
            ("2026-03-01", "1.0", uuid_of(1), "pass", 2),
            ("2026-03-02", "1.0", uuid_of(0), "pass", 24),
        ])
        t = ds.trend(project.id)
        assert t["daily"] == [2], "re-runs must not add progress"
        assert t["dates"] == ["2026-03-01"]

    def test_cumulative_is_monotonic(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, user = seeded
        uuid_of = lambda i: TestTrendHelper.uuid(project, i)
        _add_runs(project, user, [
            ("2026-03-01", "1.0", uuid_of(0), "pass", 0),
            ("2026-03-02", "1.0", uuid_of(1), "pass", 24),
            ("2026-03-03", "1.0", uuid_of(2), "pass", 48),
        ])
        t = ds.trend(project.id)
        assert t["cumulative"] == [1, 2, 3]

    def test_cancelled_runs_do_not_count_as_progress(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, user = seeded
        uuid_of = lambda i: TestTrendHelper.uuid(project, i)
        _add_runs(project, user, [
            ("2026-03-01", "1.0", uuid_of(0), "pass", 0),
            ("2026-03-01", "1.0", uuid_of(1), "cancelled", 1),
        ])
        t = ds.trend(project.id)
        assert t["daily"] == [1]

    def test_no_runs_yields_empty_series(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        t = ds.trend(project.id)
        assert t["dates"] == []
        assert t["cumulative"] == []


class TestTrendHelper:
    """Row uuids are namespaced per fixture run; recover them by index."""

    @staticmethod
    def uuid(project, index):
        from app.models import TestItemRow
        row = (TestItemRow.query
               .filter_by(project_id=project.id, sheet="test")
               .order_by(TestItemRow.id)
               .offset(index).first())
        return row.uuid


# --------------------------------------------------------------------------
# by_version
# --------------------------------------------------------------------------
class TestByVersion:
    def test_latest_run_per_case_per_version_wins(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, user = seeded
        uuid_of = lambda i: TestTrendHelper.uuid(project, i)
        _add_runs(project, user, [
            ("2026-03-01", "1.0", uuid_of(0), "fail", 0),
            ("2026-03-01", "1.0", uuid_of(0), "pass", 1),
        ])
        v = ds.by_version(project.id)
        assert v["series"]["pass"] == [1]
        assert v["series"]["fail"] == [0]

    def test_versions_are_ordered_by_first_appearance(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, user = seeded
        uuid_of = lambda i: TestTrendHelper.uuid(project, i)
        # Deliberately non-lexicographic: 9.0 ships before 10.0.
        _add_runs(project, user, [
            ("2026-03-01", "9.0", uuid_of(0), "pass", 0),
            ("2026-03-02", "10.0", uuid_of(1), "pass", 24),
        ])
        v = ds.by_version(project.id)
        assert v["versions"] == ["9.0", "10.0"], "must not sort as strings"

    def test_runs_without_a_version_get_their_own_bucket(self, seeded):
        """Unlabelled runs are surfaced, not dropped.

        A project that ran tests before it adopted version labels still did
        that work; silently discarding those runs would understate the chart
        and make executed counts disagree with the summary card.
        """
        from app.services.lanmatrix import dashboard_service as ds
        project, user = seeded
        uuid_of = lambda i: TestTrendHelper.uuid(project, i)
        _add_runs(project, user, [
            ("2026-03-01", "", uuid_of(0), "pass", 0),
            ("2026-03-01", "1.0", uuid_of(1), "pass", 1),
        ])
        v = ds.by_version(project.id)
        assert len(v["versions"]) == 2
        assert "1.0" in v["versions"]
        assert sum(v["series"]["pass"]) == 2, "no executed run may be lost"


# --------------------------------------------------------------------------
# snapshot / API shape
# --------------------------------------------------------------------------
class TestSnapshot:
    def test_returns_every_section(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        snap = ds.snapshot(project)
        assert set(snap) == {"project", "summary", "review", "trend",
                             "by_version", "review_policy"}

    def test_review_policy_defaults_are_carried(self, seeded):
        from app.services.lanmatrix import dashboard_service as ds
        project, _ = seeded
        snap = ds.snapshot(project)
        # Untestable needs review by default; PASS does not.
        assert snap["review_policy"]["untestable"] is True
        assert snap["review_policy"]["pass"] is False
