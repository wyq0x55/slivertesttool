"""Record which batch a regenerated batch was derived from."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision = "0005_batch_lineage"
down_revision = "0004_witness_handoff"
branch_labels = None
depends_on = None

_COLUMN = "derived_from_batch_id"
_TABLE = "ut_batches"
_CONSTRAINT = "fk_ut_batches_derived_from"


def reconcile_batch_lineage(bind: Connection) -> None:
    """Ensure the nullable lineage column and its foreign key on ``bind``.

    NULL means a batch was created directly; a value points at the batch whose
    confirmed witness entries it was regenerated from. Deleting the source batch
    clears the pointer rather than deleting the derived batch.
    """
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN not in have_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} INTEGER"))
    if bind.dialect.name != "postgresql":
        # SQLite only needs the column; the foreign key is a PostgreSQL contract.
        return
    have_fks = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)}
    if _CONSTRAINT in have_fks:
        return
    bind.execute(text(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        f"FOREIGN KEY ({_COLUMN}) REFERENCES {_TABLE} (id) ON DELETE SET NULL"))
    bind.execute(text(f"CREATE INDEX ix_ut_batches_derived_from ON {_TABLE} ({_COLUMN})"))


def upgrade() -> None:
    reconcile_batch_lineage(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    have_cols = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN not in have_cols:
        return
    if bind.dialect.name == "postgresql":
        bind.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}"))
        bind.execute(text("DROP INDEX IF EXISTS ix_ut_batches_derived_from"))
    bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))
