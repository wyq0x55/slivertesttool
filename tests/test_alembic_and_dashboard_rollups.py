"""Tests for embedded Alembic schema migrations and O(1) dashboard rollups."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import importlib
import os
from pathlib import Path
import random
import re
import secrets

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url


def _make_sqlite_app(monkeypatch, tmp_path: Path, db_filename: str = "rollup_test.db"):
    db_path = tmp_path / db_filename
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")
    monkeypatch.setenv("RUNNER_BACKEND", "mock")
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SECRET_KEY", "alembic-rollup-test-secret")
    monkeypatch.setenv("GLOBAL_LOGIN_REQUIRED", "0")

    import app as app_pkg
    import app.config as config_mod

    importlib.reload(config_mod)
    importlib.reload(app_pkg)
    return app_pkg.create_app(config_mod.Config)


def _reference_trend_from_raw(records, *, executed_outcomes, limit: int) -> dict:
    first_day_by_case: dict[str, str] = {}
    for rec in records:
        outcome = (rec.outcome or "").strip()
        day = str(rec.executed_on or "")
        if outcome not in executed_outcomes or day == "":
            continue
        case_key = (
            rec.row_uuid
            if (rec.row_uuid is not None and rec.row_uuid != "")
            else f"tid:{rec.test_id or ''}"
        )
        prev_day = first_day_by_case.get(case_key)
        if prev_day is None or day < prev_day:
            first_day_by_case[case_key] = day

    per_day: dict[str, int] = defaultdict(int)
    for first_day in first_day_by_case.values():
        per_day[first_day] += 1

    dates = sorted(per_day)
    if len(dates) > limit:
        dropped = dates[: len(dates) - limit]
        carried = sum(per_day[day] for day in dropped)
        dates = dates[len(dates) - limit :]
    else:
        carried = 0

    cumulative: list[int] = []
    daily: list[int] = []
    running = carried
    for day in dates:
        running += per_day[day]
        cumulative.append(running)
        daily.append(per_day[day])

    return {
        "dates": dates,
        "daily": daily,
        "cumulative": cumulative,
        "baseline": carried,
    }


def _reference_by_version_from_raw(records, *, outcomes, limit: int) -> dict:
    first_by_label: dict[str, tuple[datetime, int]] = {}
    latest_by_ver_case: dict[tuple[str, str], tuple[datetime, int, str]] = {}

    for rec in records:
        outcome = (rec.outcome or "").strip()
        if outcome not in outcomes:
            continue
        label = (rec.model_version or "").strip() or "(未标注)"
        stamp = (rec.executed_at, int(rec.id))
        if label not in first_by_label or stamp < first_by_label[label]:
            first_by_label[label] = stamp

        case_key = (
            rec.row_uuid
            if (rec.row_uuid is not None and rec.row_uuid != "")
            else f"tid:{rec.test_id or ''}"
        )
        pair = (label, case_key)
        prev = latest_by_ver_case.get(pair)
        if prev is None or stamp >= (prev[0], prev[1]):
            latest_by_ver_case[pair] = (rec.executed_at, int(rec.id), outcome)

    if not first_by_label:
        return {
            "versions": [],
            "series": {outcome: [] for outcome in outcomes},
            "folded": 0,
        }

    versions = [
        lbl
        for lbl, _stamp in sorted(
            first_by_label.items(), key=lambda pair_item: pair_item[1]
        )
    ]
    tally: dict[str, dict[str, int]] = defaultdict(
        lambda: {outcome: 0 for outcome in outcomes}
    )
    for (label, _case_key), (_exec_at, _rec_id, outcome) in latest_by_ver_case.items():
        tally[label][outcome] += 1

    folded: list[str] = []
    if len(versions) > limit:
        folded = versions[: len(versions) - limit]
        versions = versions[len(versions) - limit :]
        merged = {outcome: 0 for outcome in outcomes}
        for label in folded:
            for outcome in outcomes:
                merged[outcome] += tally[label][outcome]
        tally["其他"] = merged
        versions = ["其他"] + versions

    return {
        "versions": versions,
        "series": {
            outcome: [tally[ver_label][outcome] for ver_label in versions]
            for outcome in outcomes
        },
        "folded": len(folded),
    }


def test_reconcile_schema_sets_alembic_version_and_is_idempotent(monkeypatch, tmp_path):
    application = _make_sqlite_app(monkeypatch, tmp_path, "fresh_alembic.db")
    from app.bootstrap import reconcile_schema
    from app.extensions import db

    with application.app_context():
        reconcile_schema()
        with db.engine.connect() as conn:
            rev_first = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
        assert rev_first == ["0009_curve_view_books"]

        reconcile_schema()
        with db.engine.connect() as conn:
            rev_second = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
        assert rev_second == ["0009_curve_view_books"]

        existing_tables = set(inspect(db.engine).get_table_names())
        assert {
            "alembic_version",
            "lm_dashboard_case_first_runs",
            "lm_dashboard_daily_rollups",
            "lm_dashboard_case_version_latest",
            "lm_dashboard_version_rollups",
        } <= existing_tables
        db.engine.dispose()


def test_legacy_db_upgrade_adds_governance_columns_and_backfills_rollups(
    monkeypatch, tmp_path
):
    application = _make_sqlite_app(monkeypatch, tmp_path, "legacy_upgrade.db")
    from app.bootstrap import reconcile_schema
    from app.extensions import db
    from app.models import (
        DashboardCaseFirstRun,
        DashboardCaseVersionLatest,
        DashboardDailyRollup,
        DashboardVersionRollup,
        LMUser,
        Project,
        TestRunRecord,
    )
    from app.services.lanmatrix import dashboard_service

    with application.app_context():
        db.create_all()

        with db.engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE ut_pipeline_configs DROP COLUMN toolchain_profile")
            )
            conn.execute(text("ALTER TABLE tasks DROP COLUMN clean_room"))
            conn.execute(
                text("ALTER TABLE lm_projects DROP COLUMN default_signal_list")
            )
            conn.execute(
                text("ALTER TABLE lm_projects DROP COLUMN default_clean_room")
            )
            conn.execute(text("DROP TABLE IF EXISTS lm_dashboard_case_first_runs"))
            conn.execute(text("DROP TABLE IF EXISTS lm_dashboard_daily_rollups"))
            conn.execute(text("DROP TABLE IF EXISTS lm_dashboard_case_version_latest"))
            conn.execute(text("DROP TABLE IF EXISTS lm_dashboard_version_rollups"))
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

        owner = LMUser(username="legacy_owner", display_name="Legacy", password_hash="x")
        db.session.add(owner)
        db.session.commit()
        owner_id = owner.id

        with db.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO lm_projects (code, name, description, status, "
                    "project_type, owner_id, created_at, updated_at) "
                    "VALUES ('LEG01', 'Legacy Project', '', 'active', 'silver', "
                    ":owner_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"owner_id": owner_id},
            )
            project_id = int(
                conn.execute(
                    text("SELECT id FROM lm_projects WHERE code = 'LEG01'")
                ).scalar()
            )

        base_time = datetime(2026, 4, 10, 8, 0, 0)
        legacy_rows = [
            TestRunRecord(
                project_id=project_id,
                row_uuid="uuid-case-1",
                test_id="TC-001",
                task_key="T001",
                verdict="FAIL",
                outcome="fail",
                model_name="PlantA",
                model_version="1.0",
                executed_at=base_time,
                executed_on="2026-04-10",
            ),
            TestRunRecord(
                project_id=project_id,
                row_uuid="uuid-case-1",
                test_id="TC-001",
                task_key="T002",
                verdict="PASS",
                outcome="pass",
                model_name="PlantA",
                model_version=" 1.0 ",
                executed_at=base_time + timedelta(hours=2),
                executed_on="2026-04-11",
            ),
            TestRunRecord(
                project_id=project_id,
                row_uuid="",
                test_id="TC-002",
                task_key="T003",
                verdict="ERROR",
                outcome="error",
                model_name="PlantA",
                model_version="",
                executed_at=base_time + timedelta(days=1),
                executed_on="2026-04-11",
            ),
        ]
        db.session.add_all(legacy_rows)
        db.session.commit()

        reconcile_schema()

        inspector = inspect(db.engine)
        ut_cols = {col["name"] for col in inspector.get_columns("ut_pipeline_configs")}
        task_cols = {col["name"] for col in inspector.get_columns("tasks")}
        proj_cols = {col["name"] for col in inspector.get_columns("lm_projects")}
        assert "toolchain_profile" in ut_cols
        assert "function_step_limit" in ut_cols
        assert "clean_room" in task_cols
        assert "default_signal_list" in proj_cols
        assert "default_clean_room" in proj_cols

        with db.engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == "0009_curve_view_books"

        assert DashboardCaseFirstRun.query.filter_by(project_id=project_id).count() == 2
        assert DashboardDailyRollup.query.filter_by(project_id=project_id).count() == 2
        assert (
            DashboardCaseVersionLatest.query.filter_by(project_id=project_id).count()
            == 2
        )
        assert DashboardVersionRollup.query.filter_by(project_id=project_id).count() == 2

        trend_data = dashboard_service.trend(project_id)
        assert trend_data == {
            "dates": ["2026-04-10", "2026-04-11"],
            "daily": [1, 1],
            "cumulative": [1, 2],
            "baseline": 0,
        }
        ver_data = dashboard_service.by_version(project_id)
        assert ver_data["versions"] == ["1.0", "(未标注)"]
        assert ver_data["series"]["pass"] == [1, 0]
        assert ver_data["series"]["fail"] == [0, 0]
        assert ver_data["series"]["error"] == [0, 1]

        project = db.session.get(Project, project_id)
        assert project is not None
        assert project.default_signal_list == ""
        assert project.default_clean_room is False
        db.engine.dispose()


def test_randomized_500_runs_exact_equivalence_and_sql_budget(monkeypatch, tmp_path):
    application = _make_sqlite_app(monkeypatch, tmp_path, "randomized_500.db")
    from app.bootstrap import reconcile_schema
    from app.extensions import db
    from app.models import LMUser, Project, TestRunRecord
    from app.services.lanmatrix import dashboard_service

    with application.app_context():
        reconcile_schema()

        owner = LMUser(username="rand_owner", display_name="Rand", password_hash="x")
        db.session.add(owner)
        db.session.flush()
        project = Project(code="RAND500", name="Randomized 500", owner_id=owner.id)
        db.session.add(project)
        db.session.commit()
        project_id = project.id

        rng = random.Random(20261118)
        version_choices = [
            "",
            "   ",
            "v0.9",
            " v0.9 ",
            "v1.0",
            "v1.0  ",
            "v1.1",
            "v1.2",
            "v1.3",
            "v2.0-rc1",
            "v2.0",
            "v2.1",
            "v2.2",
            "v2.3",
            "v3.0",
            "v3.1",
            "v3.2",
            "v4.0",
        ]
        outcome_choices = [
            "pass",
            "fail",
            "error",
            "untestable",
            "cancelled",
            "blocked",
            "",
        ]
        case_pool: list[tuple[str | None, str]] = []
        for idx in range(45):
            if idx % 3 == 0:
                case_pool.append((f"row-uuid-{idx:03d}", f"TC-{idx:03d}"))
            elif idx % 3 == 1:
                case_pool.append(("", f"TC-{idx:03d}"))
            else:
                case_pool.append((None, f"CASE-{idx:03d}"))

        base_dt = datetime(2026, 1, 1, 0, 0, 0)
        generated: list[TestRunRecord] = []
        for step in range(500):
            row_uuid, test_id = rng.choice(case_pool)
            day_offset = rng.randint(0, 35)
            minute_offset = rng.randint(0, 1439)
            exec_dt = base_dt + timedelta(days=day_offset, minutes=minute_offset)
            exec_on = (
                ""
                if rng.random() < 0.06
                else (base_dt + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            )
            outcome = rng.choice(outcome_choices)
            ver_raw = rng.choice(version_choices)
            rec = TestRunRecord(
                project_id=project.id,
                row_uuid=row_uuid,
                test_id=test_id,
                task_key=f"TK{step:04d}",
                run_count=(step % 5) + 1,
                verdict=outcome.upper() if outcome else "UNKNOWN",
                outcome=outcome,
                model_name="SimModel",
                model_version=ver_raw,
                executor_id=owner.id,
                executor_name="Rand",
                executed_at=exec_dt,
                executed_on=exec_on,
            )
            generated.append(rec)

        for start in range(0, 500, 37):
            db.session.add_all(generated[start : start + 37])
            db.session.commit()

        raw_records = (
            TestRunRecord.query.filter_by(project_id=project_id)
            .order_by(TestRunRecord.id.asc())
            .all()
        )
        assert len(raw_records) == 500

        for trend_limit in (dashboard_service.MAX_TREND_POINTS, 10, 5):
            expected_trend = _reference_trend_from_raw(
                raw_records,
                executed_outcomes=dashboard_service.EXECUTED_OUTCOMES,
                limit=trend_limit,
            )
            actual_trend = dashboard_service.trend(project_id, limit=trend_limit)
            assert actual_trend == expected_trend

        for ver_limit in (dashboard_service.MAX_VERSIONS, 5, 20):
            expected_by_ver = _reference_by_version_from_raw(
                raw_records,
                outcomes=dashboard_service.OUTCOMES,
                limit=ver_limit,
            )
            actual_by_ver = dashboard_service.by_version(project_id, limit=ver_limit)
            assert actual_by_ver == expected_by_ver

        db.session.expire_all()
        executed_statements: list[str] = []

        def _capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            executed_statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", _capture_sql)
        try:
            trend_res = dashboard_service.trend(project_id)
            ver_res = dashboard_service.by_version(project_id)
        finally:
            event.remove(db.engine, "before_cursor_execute", _capture_sql)

        assert trend_res["dates"]
        assert ver_res["versions"]
        assert len(executed_statements) == 2
        lowered = [stmt.lower() for stmt in executed_statements]
        assert any("lm_dashboard_daily_rollups" in stmt for stmt in lowered)
        assert any("lm_dashboard_version_rollups" in stmt for stmt in lowered)
        for stmt in lowered:
            assert "lm_test_run_records" not in stmt
            assert "lm_dashboard_case_first_runs" not in stmt
            assert "lm_dashboard_case_version_latest" not in stmt

        db.engine.dispose()


def test_writeback_exemption_and_core_bulk_insert_maintain_rollups(
    monkeypatch, tmp_path
):
    application = _make_sqlite_app(monkeypatch, tmp_path, "hooks_and_bulk.db")
    from app.bootstrap import reconcile_schema
    from app.extensions import db
    from app.models import LMUser, Project, Task, TestItemRow, TestRunRecord
    from app.services.lanmatrix import (
        dashboard_rollup_service,
        dashboard_service,
        exemption_service,
        run_writeback_service,
    )

    with application.app_context():
        reconcile_schema()

        owner = LMUser(username="hook_owner", display_name="Hook", password_hash="x")
        db.session.add(owner)
        db.session.flush()
        project = Project(code="HOOK01", name="Hook Project", owner_id=owner.id)
        db.session.add(project)
        db.session.flush()

        row_one = TestItemRow(
            project_id=project.id,
            sheet="test",
            uuid="hook-row-1",
            case_id="TC-H1",
            title="Hook Case 1",
            result="Not Tested",
            workflow_status="Draft",
        )
        row_two = TestItemRow(
            project_id=project.id,
            sheet="test",
            uuid="hook-row-2",
            case_id="TC-H2",
            title="Hook Case 2",
            result="Not Tested",
            workflow_status="Draft",
            custom_values={"item_created": "不要"},
        )
        db.session.add_all([row_one, row_two])
        db.session.commit()

        task = Task(
            task_key="TH001",
            test_id="TC-H1",
            project_id=project.id,
            submitter_id=owner.id,
            sil_name="ModelH",
            sil_version="2.0",
            finished_at=datetime(2026, 5, 1, 10, 0, 0),
        )
        db.session.add(task)
        db.session.commit()

        assert run_writeback_service.record_run(task, "FAIL") == 1
        task.run_count = 2
        task.finished_at = datetime(2026, 5, 2, 11, 0, 0)
        db.session.commit()
        assert run_writeback_service.record_run(task, "PASS") == 1

        exemption_service.decide(
            project, row_two, True, actor_id=owner.id, note="Not needed"
        )
        db.session.commit()

        bulk_rows = [
            {
                "project_id": project.id,
                "row_uuid": "bulk-uuid-1",
                "test_id": "TC-B1",
                "task_key": "TB001",
                "run_count": 1,
                "verdict": "PASS",
                "outcome": "pass",
                "model_name": "ModelH",
                "model_version": "2.1",
                "executor_id": owner.id,
                "executor_name": "Hook",
                "executed_at": datetime(2026, 5, 3, 9, 0, 0),
                "executed_on": "2026-05-03",
            },
            {
                "project_id": project.id,
                "row_uuid": "bulk-uuid-2",
                "test_id": "TC-B2",
                "task_key": "TB002",
                "run_count": 1,
                "verdict": "ERROR",
                "outcome": "error",
                "model_name": "ModelH",
                "model_version": "2.1",
                "executor_id": owner.id,
                "executor_name": "Hook",
                "executed_at": datetime(2026, 5, 3, 10, 0, 0),
                "executed_on": "2026-05-03",
            },
        ]
        db.session.execute(TestRunRecord.__table__.insert(), bulk_rows)
        db.session.commit()

        raw_before_reset = (
            TestRunRecord.query.filter_by(project_id=project.id)
            .order_by(TestRunRecord.id.asc())
            .all()
        )
        assert dashboard_service.trend(project.id) == _reference_trend_from_raw(
            raw_before_reset,
            executed_outcomes=dashboard_service.EXECUTED_OUTCOMES,
            limit=dashboard_service.MAX_TREND_POINTS,
        )
        assert dashboard_service.by_version(project.id) == _reference_by_version_from_raw(
            raw_before_reset,
            outcomes=dashboard_service.OUTCOMES,
            limit=dashboard_service.MAX_VERSIONS,
        )

        exemption_service.reset(row_two)
        db.session.commit()

        raw_after_reset = (
            TestRunRecord.query.filter_by(project_id=project.id)
            .order_by(TestRunRecord.id.asc())
            .all()
        )
        assert dashboard_service.trend(project.id) == _reference_trend_from_raw(
            raw_after_reset,
            executed_outcomes=dashboard_service.EXECUTED_OUTCOMES,
            limit=dashboard_service.MAX_TREND_POINTS,
        )
        assert dashboard_service.by_version(project.id) == _reference_by_version_from_raw(
            raw_after_reset,
            outcomes=dashboard_service.OUTCOMES,
            limit=dashboard_service.MAX_VERSIONS,
        )

        dashboard_rollup_service.rebuild_project_rollups(project.id)
        db.session.commit()
        assert dashboard_service.by_version(project.id) == _reference_by_version_from_raw(
            raw_after_reset,
            outcomes=dashboard_service.OUTCOMES,
            limit=dashboard_service.MAX_VERSIONS,
        )
        db.engine.dispose()


def test_postgresql_disposable_schema_alembic_and_rollups(monkeypatch, tmp_path):
    from dotenv import dotenv_values
    import psycopg2

    repo_root = Path(__file__).resolve().parent.parent
    env_values = dotenv_values(repo_root / ".env")
    raw_pg_url = env_values.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not raw_pg_url or not str(raw_pg_url).startswith("postgresql"):
        pytest.skip("PostgreSQL DSN not configured for disposable-schema verification")

    base_url = make_url(str(raw_pg_url))
    try:
        admin_conn = psycopg2.connect(
            dbname=base_url.database,
            user=base_url.username,
            password=base_url.password,
            host=base_url.host,
            port=base_url.port or 5432,
            connect_timeout=5,
        )
    except Exception as exc:
        pytest.skip(f"PostgreSQL server unreachable: {exc}")

    admin_conn.autocommit = True
    schema_name = f"verify_alembic_{secrets.token_hex(6)}"
    assert re.fullmatch(r"verify_alembic_[0-9a-f]{12}", schema_name)

    with admin_conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        public_tables_before = cursor.fetchone()[0]
        cursor.execute(f"CREATE SCHEMA {schema_name}")

    try:
        scoped_url = base_url.update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        ).render_as_string(hide_password=False)

        monkeypatch.setenv("HUEY_IMMEDIATE", "1")
        monkeypatch.setenv("RUNNER_BACKEND", "mock")
        monkeypatch.setenv("GLOBAL_LOGIN_REQUIRED", "0")
        monkeypatch.setenv("INSTANCE_DIR", str(tmp_path / "pg_instance"))
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "pg_uploads"))
        monkeypatch.setenv("REPORT_DIR", str(tmp_path / "pg_reports"))
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "pg_ws"))
        monkeypatch.setenv("DATABASE_URL", scoped_url)
        monkeypatch.setenv("SECRET_KEY", "pg-disposable-schema-secret")

        import app as app_pkg
        import app.config as config_mod

        importlib.reload(config_mod)
        importlib.reload(app_pkg)

        application = app_pkg.create_app(config_mod.Config)
        from app.bootstrap import _migrate_schema, reconcile_schema
        from app.extensions import db
        from app.models import (
            DashboardDailyRollup,
            DashboardVersionRollup,
            LMUser,
            TestItemRow,
            TestRunRecord,
        )
        from app.services.lanmatrix import dashboard_service

        with application.app_context():
            for engine_obj in db.engines.values():

                @event.listens_for(engine_obj, "connect")
                def _set_schema(dbapi_conn, _conn_record):
                    with dbapi_conn.cursor() as db_cur:
                        db_cur.execute(f"SET search_path TO {schema_name}")

            db.engine.dispose()
            try:
                with db.engine.connect() as conn:
                    current_schema = conn.execute(
                        text("SELECT current_schema()")
                    ).scalar()
                assert current_schema == schema_name

                reconcile_schema()
                reconcile_schema()
                with db.engine.connect() as conn:
                    revisions = (
                        conn.execute(text("SELECT version_num FROM alembic_version"))
                        .scalars()
                        .all()
                    )
                assert revisions == ["0009_curve_view_books"]

                with db.engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE ut_pipeline_configs "
                            "DROP COLUMN toolchain_profile"
                        )
                    )
                    conn.execute(text("ALTER TABLE tasks DROP COLUMN clean_room"))
                    conn.execute(
                        text("ALTER TABLE lm_projects DROP COLUMN default_signal_list")
                    )
                    conn.execute(
                        text("ALTER TABLE lm_projects DROP COLUMN default_clean_room")
                    )
                    conn.execute(
                        text(
                            "ALTER TABLE lm_test_items "
                            "ALTER COLUMN uuid TYPE VARCHAR(32)"
                        )
                    )
                    conn.execute(
                        text(
                            "ALTER TABLE lm_data_jobs "
                            "DROP CONSTRAINT IF EXISTS lm_data_jobs_status_check"
                        )
                    )
                    conn.execute(
                        text(
                            "ALTER TABLE lm_data_jobs "
                            "ADD CONSTRAINT lm_data_jobs_status_check "
                            "CHECK (status IN ('pending', 'running', 'success', "
                            "'failed', 'cancelled'))"
                        )
                    )
                    conn.execute(
                        text("DROP TABLE lm_dashboard_case_first_runs CASCADE")
                    )
                    conn.execute(text("DROP TABLE lm_dashboard_daily_rollups CASCADE"))
                    conn.execute(
                        text("DROP TABLE lm_dashboard_case_version_latest CASCADE")
                    )
                    conn.execute(
                        text("DROP TABLE lm_dashboard_version_rollups CASCADE")
                    )
                    conn.execute(text("DROP TABLE alembic_version"))

                tester = LMUser(
                    username="pg_schema_tester",
                    display_name="PG Schema Tester",
                    password_hash="x",
                )
                db.session.add(tester)
                db.session.commit()
                tester_id = tester.id

                with db.engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO lm_projects (code, name, description, status, "
                            "project_type, owner_id, created_at, updated_at) "
                            "VALUES ('PG01', 'PG Project', '', 'active', 'silver', "
                            ":owner_id, NOW(), NOW())"
                        ),
                        {"owner_id": tester_id},
                    )
                    project_id = int(
                        conn.execute(
                            text("SELECT id FROM lm_projects WHERE code = 'PG01'")
                        ).scalar()
                    )

                base_time = datetime(2026, 6, 1, 8, 0, 0)
                db.session.add(
                    TestItemRow(
                        project_id=project_id,
                        sheet="test",
                        uuid="legacy32uuid0001",
                        case_id="TC-PG1",
                        title="",
                        comment="",
                        custom_values={
                            "test_name": "Lifted Title",
                            "remark": "Lifted Remark",
                        },
                    )
                )
                db.session.add_all(
                    [
                        TestRunRecord(
                            project_id=project_id,
                            row_uuid="legacy32uuid0001",
                            test_id="TC-PG1",
                            task_key="TPG1",
                            verdict="FAIL",
                            outcome="fail",
                            model_name="ModelPG",
                            model_version="1.0",
                            executed_at=base_time,
                            executed_on="2026-06-01",
                        ),
                        TestRunRecord(
                            project_id=project_id,
                            row_uuid="legacy32uuid0001",
                            test_id="TC-PG1",
                            task_key="TPG2",
                            verdict="PASS",
                            outcome="pass",
                            model_name="ModelPG",
                            model_version=" 1.0 ",
                            executed_at=base_time + timedelta(hours=3),
                            executed_on="2026-06-02",
                        ),
                    ]
                )
                db.session.commit()

                reconcile_schema()

                pg_inspector = inspect(db.engine)
                assert "toolchain_profile" in {
                    col_info["name"]
                    for col_info in pg_inspector.get_columns("ut_pipeline_configs")
                }
                assert "clean_room" in {
                    col_info["name"] for col_info in pg_inspector.get_columns("tasks")
                }
                assert "default_signal_list" in {
                    col_info["name"]
                    for col_info in pg_inspector.get_columns("lm_projects")
                }
                assert "default_clean_room" in {
                    col_info["name"]
                    for col_info in pg_inspector.get_columns("lm_projects")
                }
                uuid_col = next(
                    col_info
                    for col_info in pg_inspector.get_columns("lm_test_items")
                    if col_info["name"] == "uuid"
                )
                assert uuid_col["type"].length >= 64

                migrated_item = TestItemRow.query.filter_by(
                    project_id=project_id, uuid="legacy32uuid0001"
                ).one()
                assert migrated_item.title == "Lifted Title"
                assert migrated_item.comment == "Lifted Remark"
                assert "test_name" not in (migrated_item.custom_values or {})

                assert (
                    DashboardDailyRollup.query.filter_by(project_id=project_id).count()
                    == 2
                )
                assert (
                    DashboardVersionRollup.query.filter_by(
                        project_id=project_id
                    ).count()
                    == 1
                )

                db.session.execute(
                    TestRunRecord.__table__.insert(),
                    [
                        {
                            "project_id": project_id,
                            "row_uuid": "pg-case-2",
                            "test_id": "TC-PG2",
                            "task_key": "TPG3",
                            "run_count": 1,
                            "verdict": "UNTESTABLE",
                            "outcome": "untestable",
                            "model_name": "ModelPG",
                            "model_version": "",
                            "executor_id": tester_id,
                            "executor_name": "PG Schema Tester",
                            "executed_at": base_time + timedelta(days=2),
                            "executed_on": "2026-06-03",
                        }
                    ],
                )
                db.session.commit()

                all_records = (
                    TestRunRecord.query.filter_by(project_id=project_id)
                    .order_by(TestRunRecord.id.asc())
                    .all()
                )
                assert dashboard_service.trend(project_id) == _reference_trend_from_raw(
                    all_records,
                    executed_outcomes=dashboard_service.EXECUTED_OUTCOMES,
                    limit=dashboard_service.MAX_TREND_POINTS,
                )
                assert dashboard_service.by_version(
                    project_id
                ) == _reference_by_version_from_raw(
                    all_records,
                    outcomes=dashboard_service.OUTCOMES,
                    limit=dashboard_service.MAX_VERSIONS,
                )

                db.session.remove()
                with db.engine.begin() as conn:
                    conn.execute(
                        text("ALTER TABLE lm_test_run_records DROP COLUMN run_count")
                    )
                _migrate_schema()
                assert "run_count" in {
                    col_info["name"]
                    for col_info in inspect(db.engine).get_columns(
                        "lm_test_run_records"
                    )
                }
            finally:
                db.session.remove()
                db.engine.dispose()
    finally:
        with admin_conn.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            cursor.execute(
                "SELECT count(*) FROM information_schema.schemata "
                "WHERE schema_name = %s",
                (schema_name,),
            )
            schema_remaining = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
            public_tables_after = cursor.fetchone()[0]
        admin_conn.close()
        assert schema_remaining == 0
        assert public_tables_after == public_tables_before
