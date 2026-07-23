"""The shared Huey instance backed by PostgreSQL.

Both the web process (which enqueues) and the worker process (which consumes)
import this single instance. The queue is stored in PostgreSQL via
:class:`huey.contrib.sql_huey.SqlHuey` (peewee), reusing the same LAN database
server as the application data. This removes the former local SQLite queue file
*and* avoids needing a separate broker (Redis/RabbitMQ) -- matching the offline,
internal-network deployment goal while keeping everything on PostgreSQL.

Huey's own tables (task queue, schedule and result store) are created
automatically on first use, alongside the SQLAlchemy-managed application tables.
"""

from __future__ import annotations

import os
import re

from ..config import Config

Config.ensure_dirs()

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
    # Production: the queue is persisted in PostgreSQL alongside the application
    # data via peewee (huey.contrib.sql_huey). peewee connects lazily, so
    # importing this module does not require the database to be reachable yet.
    from huey.contrib.sql_huey import SqlHuey
    from playhouse.db_url import connect as _pw_connect

    _pg_database = _pw_connect(_peewee_url(Config.HUEY_DATABASE_URL))

    huey = SqlHuey(
        name=Config.HUEY_NAME,
        database=_pg_database,
        immediate=False,
    )
