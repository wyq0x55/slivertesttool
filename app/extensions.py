"""Shared Flask extensions.

Kept in a dedicated module so both the application factory and the standalone
Huey worker can import the same ``db`` instance without creating an import
cycle through :mod:`app`.

The platform runs exclusively on PostgreSQL. Connection health and pooling are
configured via ``SQLALCHEMY_ENGINE_OPTIONS`` in :class:`app.config.Config`
(``pool_pre_ping`` / ``pool_recycle`` etc.); no per-connection PRAGMA tuning is
needed the way the removed SQLite/WAL backend required.
"""

from __future__ import annotations

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event

db = SQLAlchemy()


def configure_utc_connections(app: Flask) -> None:
    """Keep PostgreSQL timestamp casts in UTC without changing stored data."""
    with app.app_context():
        for engine in db.engines.values():
            if engine.dialect.name == "postgresql":
                event.listen(engine, "do_connect", _utc_connect_parameters)


def _utc_connect_parameters(_dialect, _record, _arguments, parameters) -> None:
    options = parameters.get("options") or ""
    parameters["options"] = f"{options} -c timezone=UTC".strip()
