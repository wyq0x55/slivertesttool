"""Add the declared per-function semantic step ceiling to the UT configuration."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision = "0003_step_budget"
down_revision = "0002_rollups"
branch_labels = None
depends_on = None

_COLUMN = "function_step_limit"
_TABLE = "ut_pipeline_configs"


def reconcile_step_budget(bind: Connection) -> None:
    """Ensure the nullable ceiling column on ``bind``.

    NULL is meaningful: the configuration declares no ceiling and ut-agent keeps
    its own default, so the column carries no server-side default.
    """
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in have_cols:
        return
    bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} INTEGER"))


def upgrade() -> None:
    reconcile_step_budget(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in have_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))