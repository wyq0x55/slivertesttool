"""Add governance columns and O(1) incremental dashboard rollup tables."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection

from app.models.lanmatrix import (
    DashboardCaseFirstRun,
    DashboardCaseVersionLatest,
    DashboardDailyRollup,
    DashboardVersionRollup,
    TestRunRecord,
)
from app.services.lanmatrix.dashboard_rollup_service import backfill_all_rollups

revision = "0002_rollups"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_ROLLUP_MODELS = (
    DashboardCaseFirstRun,
    DashboardDailyRollup,
    DashboardCaseVersionLatest,
    DashboardVersionRollup,
)


def reconcile_governance_and_rollups(bind: Connection) -> None:
    """Ensure governance columns, rollup tables, and initial backfill on ``bind``."""
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    is_pg = bind.dialect.name == "postgresql"

    governance_columns = {
        "ut_pipeline_configs": {
            "toolchain_profile": "VARCHAR(64) NOT NULL DEFAULT 'default'",
        },
        "tasks": {
            "clean_room": (
                "BOOLEAN NOT NULL DEFAULT FALSE"
                if is_pg
                else "BOOLEAN NOT NULL DEFAULT 0"
            ),
        },
        "lm_projects": {
            "default_signal_list": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table_name, columns in governance_columns.items():
        if table_name not in existing_tables:
            continue
        have_cols = {col["name"] for col in inspector.get_columns(table_name)}
        for col_name, ddl in columns.items():
            if col_name in have_cols:
                continue
            bind.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {ddl}"))

    for model_cls in _ROLLUP_MODELS:
        model_cls.__table__.create(bind=bind, checkfirst=True)

    bind.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_dash_ver_rollup_proj_seen "
            "ON lm_dashboard_version_rollups (project_id, first_seen_at, first_seen_id)"
        )
    )

    if "lm_test_run_records" in existing_tables and "lm_projects" in existing_tables:
        rollups_empty = all(
            bind.execute(select(model_cls.__table__.c.project_id).limit(1)).first()
            is None
            for model_cls in _ROLLUP_MODELS
        )
        if rollups_empty:
            record_table = TestRunRecord.__table__
            has_records = (
                bind.execute(select(record_table.c.id).limit(1)).first() is not None
            )
            if has_records:
                backfill_all_rollups(conn=bind)


def upgrade() -> None:
    reconcile_governance_and_rollups(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    for model_cls in reversed(_ROLLUP_MODELS):
        model_cls.__table__.drop(bind=bind, checkfirst=True)
