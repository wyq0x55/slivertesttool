"""Add the per-job witness handoff record."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision = "0004_witness_handoff"
down_revision = "0003_step_budget"
branch_labels = None
depends_on = None

_COLUMN = "witness"
_TABLE = "ut_jobs"


def reconcile_witness_handoff(bind: Connection) -> None:
    """Ensure the nullable witness column on ``bind``.

    NULL is meaningful: generation has not frozen a handoff record for the job
    yet, which is different from a frozen record that says "no triage input".
    """
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in have_cols:
        return
    json_ddl = "JSONB" if bind.dialect.name == "postgresql" else "JSON"
    bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} {json_ddl}"))


def upgrade() -> None:
    reconcile_witness_handoff(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in have_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))
