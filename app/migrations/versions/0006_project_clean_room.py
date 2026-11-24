"""Persist the 车规洁净室 switch on the project."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision = "0006_project_clean_room"
down_revision = "0005_batch_lineage"
branch_labels = None
depends_on = None

_TABLE = "lm_projects"
_COLUMN = "default_clean_room"


def reconcile_project_clean_room(bind: Connection) -> None:
    """Ensure ``default_clean_room`` exists on ``bind``.

    The switch used to be a checkbox on the matrix page, so nothing outlived a
    page visit and every queued run reported ``clean_room=false``. Existing
    projects keep that behaviour: ``DEFAULT FALSE`` makes the column non-null on
    an already-populated table without a backfill pass.
    """
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in have_cols:
        return
    bind.execute(text(
        f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} BOOLEAN NOT NULL DEFAULT FALSE"))


def upgrade() -> None:
    reconcile_project_clean_room(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN not in have_cols:
        return
    bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))