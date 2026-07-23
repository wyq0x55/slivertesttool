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

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
