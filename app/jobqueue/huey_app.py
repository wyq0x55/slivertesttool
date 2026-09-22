"""The shared Huey instance backed by PostgreSQL.

Both the web process (which enqueues) and the worker process (which consumes)
import this single instance. The queue is stored in PostgreSQL via
:class:`huey.contrib.sql_huey.SqlHuey` (peewee), reusing the same LAN database
server as the application data. This removes the former local SQLite queue file
*and* avoids needing a separate broker (Redis/RabbitMQ) -- matching the offline,
internal-network deployment goal while keeping everything on PostgreSQL.

Huey's own tables (task queue, schedule and result store) are also owned by the
explicit platform bootstrap; importing this module performs no DDL.
"""

from __future__ import annotations

import os
import re

from ..config import Config

# ``immediate`` runs tasks synchronously in-process; handy for tests. Enabled by
# setting HUEY_IMMEDIATE=1 in the environment.
_immediate = os.environ.get("HUEY_IMMEDIATE", "").strip().lower() in (
    "1", "true", "yes", "on"
)


def _peewee_url(sqlalchemy_url: str) -> str:
    """Translate a SQLAlchemy DSN into a peewee ``playhouse.db_url`` DSN.

    SQLAlchemy encodes the driver in the scheme (``postgresql+psycopg2://``),
    which peewee does not understand; strip the ``+driver`` suffix so peewee
    sees a plain ``postgresql://`` (or ``postgres://``) URL.
    """
    return re.sub(
        r"^(postgresql|postgres)\+[A-Za-z0-9_]+://", r"\1://", sqlalchemy_url
    )


if _immediate:
    # Synchronous, in-memory queue for tests / one-shot runs: tasks execute
    # inline so no PostgreSQL queue storage (or connection) is involved.
    from huey import MemoryHuey

    huey = MemoryHuey(name=Config.HUEY_NAME, immediate=True)
else:
    # Huey 2.5 SqlStorage normally creates its three tables from __init__.
    # That makes any web/worker import a schema owner, which conflicts with the
    # platform's explicit one-owner bootstrap. Suppress only that constructor
    # DDL; bootstrap_app() calls ensure_huey_schema() before services start.
    from huey.api import Huey
    from huey.contrib.sql_huey import SqlStorage
    from playhouse.db_url import connect as _pw_connect

    class _BootstrapOwnedSqlStorage(SqlStorage):
        def create_tables(self):
            return None

        def initialize_tables(self) -> None:
            super().create_tables()

    _pg_database = _pw_connect(_peewee_url(Config.HUEY_DATABASE_URL))

    huey = Huey(
        name=Config.HUEY_NAME,
        storage_class=_BootstrapOwnedSqlStorage,
        database=_pg_database,
        immediate=False,
    )


def ensure_huey_schema() -> None:
    """Create Huey's SQL tables from the explicit bootstrap owner only."""
    storage = getattr(huey, "storage", None)
    initialize = getattr(storage, "initialize_tables", None)
    if initialize is not None:
        initialize()
