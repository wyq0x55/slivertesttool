from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision = "0008_test_books"
down_revision = "0007_budget_stop"
branch_labels = None
depends_on = None


def _add_column(bind: Connection, table: str, name: str, ddl: str) -> None:
    inspector = inspect(bind)
    if table not in set(inspector.get_table_names()):
        return
    if name in {col["name"] for col in inspector.get_columns(table)}:
        return
    bind.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def reconcile_test_books(bind: Connection) -> None:
    """Create the book table and add nullable scoping columns.

    All existing rows keep ``book_id = NULL``; this is why the new columns are
    nullable. Existing silver projects therefore need no data migration.
    The model table itself is created by ``db.create_all()`` on a fresh
    database; this function makes the same state arrive on an already-migrated
    database.
    """
    from app.models import TestBook

    inspector = inspect(bind)
    if TestBook.__tablename__ not in set(inspector.get_table_names()):
        TestBook.__table__.create(bind, checkfirst=True)

    is_pg = bind.dialect.name == "postgresql"
    book_fk = "INTEGER REFERENCES lm_test_books(id) ON DELETE CASCADE" if is_pg else "INTEGER"
    run_book_fk = (
        "INTEGER REFERENCES lm_test_books(id) ON DELETE SET NULL"
        if is_pg else "INTEGER"
    )
    _add_column(bind, "lm_test_items", "book_id", book_fk)
    _add_column(bind, "tasks", "book_id", "INTEGER")
    _add_column(bind, "tasks", "row_uuid", "VARCHAR(64)")
    _add_column(bind, "lm_test_run_records", "book_id", run_book_fk)

    inspector = inspect(bind)
    item_indexes = {idx["name"] for idx in inspector.get_indexes("lm_test_items")}
    if "ix_item_project_book_sheet" not in item_indexes:
        bind.execute(text(
            "CREATE INDEX ix_item_project_book_sheet "
            "ON lm_test_items (project_id, book_id, sheet)"
        ))
    task_indexes = {idx["name"] for idx in inspector.get_indexes("tasks")}
    if "ix_tasks_book_id" not in task_indexes:
        bind.execute(text("CREATE INDEX ix_tasks_book_id ON tasks (book_id)"))
    if "ix_tasks_row_uuid" not in task_indexes:
        bind.execute(text("CREATE INDEX ix_tasks_row_uuid ON tasks (row_uuid)"))

    if "uq_item_book_sheet_case" not in item_indexes:
        bind.execute(text(
            "CREATE UNIQUE INDEX uq_item_book_sheet_case "
            "ON lm_test_items (book_id, sheet, case_id) "
            "WHERE book_id IS NOT NULL AND deleted_at IS NULL"
        ))


def upgrade() -> None:
    reconcile_test_books(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    try:
        bind.execute(text("DROP INDEX IF EXISTS uq_item_book_sheet_case"))
    except Exception:  # noqa: BLE001 - best-effort downgrade
        pass
    for table, column in (
            ("lm_test_items", "book_id"),
            ("tasks", "book_id"),
            ("tasks", "row_uuid"),
            ("lm_test_run_records", "book_id"),
    ):
        try:
            bind.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        except Exception:  # noqa: BLE001 - best-effort downgrade
            pass
    try:
        bind.execute(text("DROP TABLE lm_test_books"))
    except Exception:  # noqa: BLE001 - best-effort downgrade
        pass
