"""Scope saved curve views by book while retaining legacy layouts."""

from __future__ import annotations

from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, inspect, text
from sqlalchemy.engine import Connection

revision = "0009_curve_view_books"
down_revision = "0008_test_books"
branch_labels = None
depends_on = None

_TABLE = "curve_views"
_LEGACY_NAME = "uq_curve_view_name"
_BOOK_NAME = "uq_curve_view_book_name"
_LEGACY_COLUMNS = ["project_id", "test_id", "name"]


def reconcile_curve_view_books(bind: Connection) -> None:
    inspector = inspect(bind)
    if not inspector.has_table(_TABLE):
        return

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints(_TABLE)}
    operations = Operations(MigrationContext.configure(bind))
    if "book_id" not in columns or _LEGACY_NAME in constraints:
        with operations.batch_alter_table(_TABLE) as batch:
            if "book_id" not in columns:
                batch.add_column(Column("book_id", Integer))
            if _LEGACY_NAME in constraints:
                batch.drop_constraint(_LEGACY_NAME, type_="unique")

    indexes = {index["name"] for index in inspect(bind).get_indexes(_TABLE)}
    if _LEGACY_NAME not in indexes:
        predicate = text("book_id IS NULL")
        operations.create_index(_LEGACY_NAME, _TABLE, _LEGACY_COLUMNS, unique=True,
                                postgresql_where=predicate, sqlite_where=predicate)
    if _BOOK_NAME not in indexes:
        predicate = text("book_id IS NOT NULL")
        operations.create_index(_BOOK_NAME, _TABLE, ["project_id", "book_id", "test_id", "name"],
                                unique=True, postgresql_where=predicate, sqlite_where=predicate)


def upgrade() -> None:
    reconcile_curve_view_books(op.get_bind())


def downgrade() -> None:
    op.drop_index(_BOOK_NAME, table_name=_TABLE)
    op.drop_index(_LEGACY_NAME, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.create_unique_constraint(_LEGACY_NAME, _LEGACY_COLUMNS)
        batch.drop_column("book_id")
