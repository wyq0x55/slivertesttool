"""Baseline schema reconciliation for legacy pre-Alembic databases."""

from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import inspect, select, text, update
from sqlalchemy.engine import Connection

from app.extensions import db
from app.models import TestItemRow

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def reconcile_baseline_schema(bind: Connection) -> None:
    """Apply idempotent pre-Alembic schema and constraint upgrades on ``bind``."""
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    is_pg = bind.dialect.name == "postgresql"
    json_ddl = "JSONB" if is_pg else "JSON"
    bool_false_ddl = (
        "BOOLEAN NOT NULL DEFAULT FALSE" if is_pg else "BOOLEAN NOT NULL DEFAULT 0"
    )
    stop_by_ddl = (
        "INTEGER REFERENCES lm_users(id) ON DELETE SET NULL" if is_pg else "INTEGER"
    )

    additions = {
        "lm_test_run_records": {
            "run_count": "INTEGER",
        },
        "tasks": {
            "sil_name": "VARCHAR(128) NOT NULL DEFAULT ''",
            "project_id": "INTEGER",
            "submitter_id": "INTEGER",
            "deleted_at": "TIMESTAMP",
            "deleted_by": "INTEGER",
            "run_count": "INTEGER NOT NULL DEFAULT 1",
            "sil_version": "VARCHAR(64) NOT NULL DEFAULT ''",
            "sil_model_id": "INTEGER",
        },
        "lm_projects": {
            "project_type": "VARCHAR(32) NOT NULL DEFAULT 'silver'",
            "tm_id_prefix": "VARCHAR(64)",
            "tm_summary_sheet": "VARCHAR(120)",
            "review_required_on": json_ddl,
            "default_reviewer_id": "INTEGER",
            "review_routes": json_ddl,
        },
        "lm_notifications": {
            "read_at": "TIMESTAMP",
            "archived_at": "TIMESTAMP",
        },
        "lm_field_definitions": {
            "sheet": "VARCHAR(16) NOT NULL DEFAULT 'test'",
            "deleted_at": "TIMESTAMP",
            "deleted_by": "INTEGER",
        },
        "lm_test_items": {
            "sheet": "VARCHAR(16) NOT NULL DEFAULT 'test'",
            "deleted_by": "INTEGER",
            "review_status": "VARCHAR(16) NOT NULL DEFAULT ''",
            "reviewer_id": "INTEGER",
            "review_note": "TEXT NOT NULL DEFAULT ''",
            "review_requested_at": "TIMESTAMP",
            "review_requested_by": "INTEGER",
            "reviewed_at": "TIMESTAMP",
            "review_verdict": "VARCHAR(24) NOT NULL DEFAULT ''",
            "exempt_status": "VARCHAR(16) NOT NULL DEFAULT ''",
            "exempt_value": "VARCHAR(24) NOT NULL DEFAULT ''",
            "exempt_note": "TEXT NOT NULL DEFAULT ''",
            "exempt_reviewer_id": "INTEGER",
            "exempt_requested_at": "TIMESTAMP",
            "exempt_requested_by": "INTEGER",
            "exempt_decided_at": "TIMESTAMP",
        },
        "lm_project_models": {
            "is_current": bool_false_ddl,
            "version": "VARCHAR(64)",
            "version_note": "TEXT NOT NULL DEFAULT ''",
            "deprecated_at": "TIMESTAMP",
        },
        "ut_pipeline_configs": {
            "execution_backend": "VARCHAR(16) NOT NULL DEFAULT 'clang'",
            "stub_mode": "VARCHAR(16) NOT NULL DEFAULT 'deterministic'",
            "stub_model": "VARCHAR(128) NOT NULL DEFAULT ''",
            "stub_max_rounds": "INTEGER NOT NULL DEFAULT 2",
            "host_clang": "VARCHAR(1024) NOT NULL DEFAULT 'clang'",
            "host_llvm_cov": "VARCHAR(1024) NOT NULL DEFAULT 'llvm-cov'",
            "host_llvm_profdata": "VARCHAR(1024) NOT NULL DEFAULT 'llvm-profdata'",
            "host_profile_runtime": "VARCHAR(1024) NOT NULL DEFAULT ''",
        },
        "ut_jobs": {
            "gate_code": "VARCHAR(32) NOT NULL DEFAULT ''",
            "stop_requested_at": "TIMESTAMP",
            "stop_requested_by": stop_by_ddl,
        },
    }
    for table_name, columns in additions.items():
        if table_name not in existing_tables:
            continue
        have_cols = {col["name"] for col in inspector.get_columns(table_name)}
        for col_name, ddl in columns.items():
            if col_name in have_cols:
                continue
            bind.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {ddl}"))

    retired = {"ut_pipeline_configs": ("work_root", "stub_builds")}
    for table_name, columns in retired.items():
        if table_name not in existing_tables:
            continue
        have_cols = {col["name"] for col in inspector.get_columns(table_name)}
        for col_name in columns:
            if col_name not in have_cols:
                continue
            bind.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {col_name}"))

    _migrate_model_version_identity(bind, existing_tables)
    _ensure_legacy_indexes(bind, existing_tables)

    refreshed_inspector = inspect(bind)
    _migrate_widen_testitem_uuid(bind, existing_tables, refreshed_inspector)
    _reconcile_user_fk_ondelete(bind, existing_tables, refreshed_inspector)
    _migrate_testitem_field_keys(bind, existing_tables)
    _migrate_data_job_status_check(bind, existing_tables)


def _migrate_model_version_identity(bind: Connection, existing_tables: set[str]) -> None:
    if "lm_project_models" not in existing_tables or bind.dialect.name != "postgresql":
        return
    bind.execute(
        text("ALTER TABLE lm_project_models DROP CONSTRAINT IF EXISTS uq_model_project_name")
    )
    bind.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_project_name_version_norm "
            "ON lm_project_models (project_id, name, COALESCE(version, ''))"
        )
    )


def _ensure_legacy_indexes(bind: Connection, existing_tables: set[str]) -> None:
    for table_name in ("lm_field_definitions", "lm_test_items"):
        if table_name in existing_tables:
            bind.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_sheet ON {table_name} (sheet)")
            )

    for table_name in ("tasks", "lm_field_definitions"):
        if table_name in existing_tables:
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table_name}_deleted_at "
                    f"ON {table_name} (deleted_at)"
                )
            )

    if "lm_notifications" in existing_tables:
        for col_name in ("read_at", "archived_at"):
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_notif_{col_name} "
                    f"ON lm_notifications ({col_name})"
                )
            )

    if "lm_test_items" in existing_tables:
        for col_name in ("exempt_status", "exempt_reviewer_id", "exempt_requested_by"):
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_lm_items_{col_name} "
                    f"ON lm_test_items ({col_name})"
                )
            )

    if "lm_test_run_records" in existing_tables:
        is_pg = bind.dialect.name == "postgresql"
        ver_idx_cols = (
            "(project_id, model_version, row_uuid, executed_at DESC, id DESC, outcome)"
            if is_pg
            else "(project_id, model_version, row_uuid, executed_at, id, outcome)"
        )
        ver_tid_cols = (
            "(project_id, model_version, test_id, executed_at DESC, id DESC, outcome)"
            if is_pg
            else "(project_id, model_version, test_id, executed_at, id, outcome)"
        )
        cmp_idx_cols = (
            "(project_id, model_name, model_version, test_id, executed_at DESC, id DESC)"
            if is_pg
            else "(project_id, model_name, model_version, test_id, executed_at, id)"
        )
        bind.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_runrec_proj_ver_case_time "
                f"ON lm_test_run_records {ver_idx_cols}"
            )
        )
        bind.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_runrec_proj_ver_tid_time "
                f"ON lm_test_run_records {ver_tid_cols}"
            )
        )
        bind.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_runrec_proj_trend "
                "ON lm_test_run_records "
                "(project_id, outcome, row_uuid, test_id, executed_on)"
            )
        )
        bind.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_runrec_proj_model_ver_test "
                f"ON lm_test_run_records {cmp_idx_cols}"
            )
        )


def _migrate_widen_testitem_uuid(
    bind: Connection,
    existing_tables: set[str],
    inspector,
) -> None:
    if "lm_test_items" not in existing_tables or bind.dialect.name != "postgresql":
        return
    uuid_col = next(
        (col for col in inspector.get_columns("lm_test_items") if col["name"] == "uuid"),
        None,
    )
    if uuid_col is None:
        return
    length = getattr(uuid_col.get("type"), "length", None)
    if length is not None and length >= 64:
        return
    bind.execute(text("ALTER TABLE lm_test_items ALTER COLUMN uuid TYPE VARCHAR(64)"))
    logger.info("schema migration: widened lm_test_items.uuid to VARCHAR(64)")


def _reconcile_user_fk_ondelete(
    bind: Connection,
    existing_tables: set[str],
    inspector,
) -> None:
    if bind.dialect.name != "postgresql" or "lm_users" not in existing_tables:
        return

    for table in db.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        existing_fks = inspector.get_foreign_keys(table.name)
        for column in table.columns:
            user_fks = [
                fk for fk in column.foreign_keys if fk.column.table.name == "lm_users"
            ]
            if not user_fks:
                continue
            if len(user_fks) != 1:
                raise RuntimeError(
                    f"ambiguous lm_users FK policy: {table.name}.{column.name}"
                )
            model_fk = user_fks[0]
            action = (model_fk.ondelete or "NO ACTION").upper()
            if action not in {"SET NULL", "CASCADE"}:
                raise RuntimeError(
                    f"lm_users FK must declare ondelete policy: {table.name}.{column.name}"
                )
            matching = [
                fk
                for fk in existing_fks
                if fk.get("referred_table") == "lm_users"
                and list(fk.get("constrained_columns") or []) == [column.name]
            ]
            constraint_name = None
            if matching:
                current = (
                    (matching[0].get("options") or {}).get("ondelete") or "NO ACTION"
                ).upper()
                constraint_name = matching[0].get("name")
                if current == action:
                    continue

            qtable = f'"{table.name}"'
            qcol = f'"{column.name}"'
            if action == "SET NULL":
                bind.execute(
                    text(
                        f"UPDATE {qtable} SET {qcol} = NULL "
                        f"WHERE {qcol} IS NOT NULL AND NOT EXISTS "
                        f"(SELECT 1 FROM lm_users usr WHERE usr.id = {qtable}.{qcol})"
                    )
                )
            else:
                bind.execute(
                    text(
                        f"DELETE FROM {qtable} WHERE {qcol} IS NOT NULL "
                        f"AND NOT EXISTS "
                        f"(SELECT 1 FROM lm_users usr WHERE usr.id = {qtable}.{qcol})"
                    )
                )

            if constraint_name:
                bind.execute(
                    text(f'ALTER TABLE {qtable} DROP CONSTRAINT "{constraint_name}"')
                )

            desired_name = (
                model_fk.constraint.name or f"fk_{table.name}_{column.name}_lm_users"
            )
            bind.execute(
                text(
                    f'ALTER TABLE {qtable} ADD CONSTRAINT "{desired_name}" '
                    f"FOREIGN KEY ({qcol}) REFERENCES lm_users(id) ON DELETE {action}"
                )
            )
            logger.info(
                "schema migration: %s.%s FK -> ON DELETE %s",
                table.name,
                column.name,
                action,
            )


def _migrate_testitem_field_keys(bind: Connection, existing_tables: set[str]) -> None:
    if "lm_test_items" not in existing_tables:
        return
    aliases = TestItemRow._FIELD_ALIASES
    alias_keys = list(aliases)
    item_table = TestItemRow.__table__

    stmt = select(
        item_table.c.id,
        item_table.c.title,
        item_table.c.comment,
        item_table.c.custom_values,
    )
    if bind.dialect.name == "postgresql":
        stmt = stmt.where(
            text("jsonb_exists_any(custom_values, :alias_keys)").bindparams(
                alias_keys=alias_keys
            )
        )
    else:
        stmt = stmt.where(item_table.c.custom_values.isnot(None))

    rows = bind.execute(stmt).all()
    changed = 0
    for row_id, title_val, comment_val, custom_values in rows:
        if not isinstance(custom_values, dict):
            continue
        updated_custom = dict(custom_values)
        new_cols = {
            "title": title_val or "",
            "comment": comment_val or "",
        }
        touched = False
        for alias_key, col_name in aliases.items():
            if alias_key not in updated_custom:
                continue
            lifted_val = updated_custom.pop(alias_key)
            if not (new_cols.get(col_name) or "").strip():
                new_cols[col_name] = "" if lifted_val is None else str(lifted_val)
            touched = True
        if touched:
            bind.execute(
                update(item_table)
                .where(item_table.c.id == row_id)
                .values(
                    title=new_cols["title"],
                    comment=new_cols["comment"],
                    custom_values=updated_custom,
                )
            )
            changed += 1

    if changed:
        logger.info("field-key migration: unified %d legacy row(s)", changed)


def _migrate_data_job_status_check(bind: Connection, existing_tables: set[str]) -> None:
    if "lm_data_jobs" not in existing_tables or bind.dialect.name != "postgresql":
        return
    current_def = bind.execute(
        text(
            "SELECT pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conrelid = to_regclass('lm_data_jobs') "
            "  AND conname = 'lm_data_jobs_status_check'"
        )
    ).scalar()
    if current_def and "'previewed'" in current_def and "'completed'" in current_def:
        return
    bind.execute(
        text("ALTER TABLE lm_data_jobs DROP CONSTRAINT IF EXISTS lm_data_jobs_status_check")
    )
    bind.execute(
        text(
            "ALTER TABLE lm_data_jobs "
            "ADD CONSTRAINT lm_data_jobs_status_check "
            "CHECK (status IN ('pending', 'running', 'previewed', 'completed', "
            "'success', 'failed', 'cancelled'))"
        )
    )


def upgrade() -> None:
    reconcile_baseline_schema(op.get_bind())


def downgrade() -> None:
    pass
