"""Programmatic Alembic migration runner."""

from __future__ import annotations

import importlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Connection, Engine

HEAD_REVISION = "0005_batch_lineage"
_MIGRATIONS_DIR = Path(__file__).resolve().parent


def get_alembic_config(connection: Connection | None = None) -> Config:
    """Build a programmatic Alembic ``Config`` rooted at ``app/migrations``."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _reconcile_head_invariants(conn: Connection) -> None:
    baseline_mod = importlib.import_module(
        "app.migrations.versions.0001_baseline_schema"
    )
    rollups_mod = importlib.import_module(
        "app.migrations.versions.0002_governance_and_dashboard_rollups"
    )
    step_budget_mod = importlib.import_module(
        "app.migrations.versions.0003_step_budget"
    )
    witness_mod = importlib.import_module(
        "app.migrations.versions.0004_witness_handoff"
    )
    lineage_mod = importlib.import_module(
        "app.migrations.versions.0005_batch_lineage"
    )
    baseline_mod.reconcile_baseline_schema(conn)
    rollups_mod.reconcile_governance_and_rollups(conn)
    step_budget_mod.reconcile_step_budget(conn)
    witness_mod.reconcile_witness_handoff(conn)
    lineage_mod.reconcile_batch_lineage(conn)


def _run_with_connection(conn: Connection, *, force_reconcile: bool = False) -> str:
    cfg = get_alembic_config(conn)
    current_rev = MigrationContext.configure(conn).get_current_revision()

    if force_reconcile:
        if current_rev is not None:
            command.stamp(cfg, "base")
        command.upgrade(cfg, "head")
    elif current_rev != HEAD_REVISION:
        command.upgrade(cfg, "head")
    else:
        _reconcile_head_invariants(conn)

    final_rev = MigrationContext.configure(conn).get_current_revision()
    return final_rev or ""


def run_migrations(
    engine_or_conn: Engine | Connection,
    *,
    force_reconcile: bool = False,
) -> str:
    """Upgrade ``engine_or_conn`` to Alembic ``head`` and return the active revision."""
    if isinstance(engine_or_conn, Connection):
        if engine_or_conn.in_transaction():
            return _run_with_connection(
                engine_or_conn, force_reconcile=force_reconcile
            )
        with engine_or_conn.begin():
            return _run_with_connection(
                engine_or_conn, force_reconcile=force_reconcile
            )

    with engine_or_conn.begin() as conn:
        return _run_with_connection(conn, force_reconcile=force_reconcile)
