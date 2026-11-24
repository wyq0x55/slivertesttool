"""Add the declared forecast stop count to the UT configuration."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision = "0007_budget_stop"
down_revision = "0006_project_clean_room"
branch_labels = None
depends_on = None

_COLUMN = "budget_stop_observations"
_TABLE = "ut_pipeline_configs"


def reconcile_budget_stop(bind: Connection) -> None:
    """Ensure the nullable forecast stop count on ``bind``.

    NULL is meaningful: the configuration asks for no early stop and forecasts
    stay advisory, so the column carries no server-side default.
    """
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in have_cols:
        return
    bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} INTEGER"))


def upgrade() -> None:
    reconcile_budget_stop(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN not in have_cols:
        return
    bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))