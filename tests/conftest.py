"""pytest fixtures: a Flask app + client backed by a PostgreSQL test database.

The platform runs exclusively on PostgreSQL, so the tests need a reachable
PostgreSQL instance. Point them at it with ``TEST_DATABASE_URL``; if it is not
set a local default DSN is used. ``DATABASE_URL`` is deliberately ignored. The
target database is emptied at the start of each test for isolation, so its name
must end with ``_test``. Under ``pytest -n`` each worker uses its own database
derived from that name (see ``pytest_configure``).

Huey runs in immediate mode so enqueued tasks execute synchronously in-process
(via an in-memory queue), letting the API tests exercise the full
upload -> queue -> run -> result flow without a persistent queue.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ``app.jobqueue.huey_app`` picks its queue once, at import. A test module that
# imports the routes at collection time (before any fixture runs) would
# otherwise build the persistent Huey bound to the .env database: tasks are
# then queued into the real queue instead of executing inline, and every later
# pipeline test sees its jobs stuck in ``queued``. Decide it before collection.
os.environ["HUEY_IMMEDIATE"] = "1"

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg2://postgres:postgres@localhost:5432/silvetestapp_test"
)


def _require_disposable_database(url: str) -> None:
    """Refuse to wipe a database whose name does not mark it as a test one."""
    from sqlalchemy.engine import make_url

    name = make_url(url).database or ""
    if not name.endswith("_test"):
        pytest.exit(
            f"Refusing to drop all tables in database {name!r}: the test "
            "database name must end with '_test' (set TEST_DATABASE_URL).",
            returncode=2,
        )


def pytest_configure(config):
    """Give each pytest-xdist worker its own database next to the base one.

    Every worker resets its database between tests, so workers sharing one
    would wipe each other's rows. ``silvetestapp_test`` becomes
    ``silvetestapp_gw0_test`` and so on; the base URL's user must be allowed
    to create databases. DATABASE_URL / HUEY_DATABASE_URL follow only when
    they named the base test database (as CI sets them).
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    _require_disposable_database(base)
    base_url = make_url(base)
    name = f"{base_url.database[:-len('_test')]}_{worker}_test"
    engine = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}).scalar()
            if not exists:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        engine.dispose()
    worker_url = base_url.set(database=name).render_as_string(hide_password=False)
    for variable in ("DATABASE_URL", "HUEY_DATABASE_URL"):
        if os.environ.get(variable) == base:
            os.environ[variable] = worker_url
    os.environ["TEST_DATABASE_URL"] = worker_url


_schema_rebuilt = False


def _reset_database() -> None:
    """Leave every model table empty with its id sequences restarted.

    The first reset of a session drops the schema so it is rebuilt from the
    current models. Later resets keep the tables and delete their rows instead:
    re-running ~130 DDL statements per test was the dominant fixture cost, and
    the row-level reset touches exactly the tables ``drop_all`` would.
    """
    global _schema_rebuilt
    from sqlalchemy import text

    from app import models  # noqa: F401
    from app.extensions import db

    if not _schema_rebuilt:
        db.drop_all()
        _schema_rebuilt = True
        return
    tables = db.metadata.sorted_tables
    with db.engine.begin() as connection:
        sequences = connection.execute(text(
            "SELECT seq.relname FROM pg_class seq "
            "JOIN pg_depend dep ON dep.objid = seq.oid AND dep.deptype IN ('a', 'i') "
            "JOIN pg_class tbl ON tbl.oid = dep.refobjid "
            "WHERE seq.relkind = 'S' AND tbl.relname = ANY(:names)"),
            {"names": [table.name for table in tables]}).scalars().all()
        statements = [f'DELETE FROM "{table.name}"' for table in reversed(tables)]
        statements += [f'ALTER SEQUENCE "{name}" RESTART' for name in sequences]
        connection.exec_driver_sql("; ".join(statements))


@pytest.fixture()
def app_ctx(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="stp_test_")
    root = Path(tmp)
    # Never fall back to DATABASE_URL: app.config loads .env at import time, so
    # that variable usually names the developer's real database, and this
    # fixture drops every table in its target.
    test_db_url = os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    _require_disposable_database(test_db_url)
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")
    monkeypatch.setenv("RUNNER_BACKEND", "mock")
    monkeypatch.setenv("INSTANCE_DIR", str(root / "instance"))
    monkeypatch.setenv("UPLOAD_DIR", str(root / "uploads"))
    monkeypatch.setenv("REPORT_DIR", str(root / "reports"))
    monkeypatch.setenv("WORKSPACE_DIR", str(root / "ws"))
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("LICENSE_LIMIT", "2")
    # Most tests call API routes directly; authentication behavior is covered
    # separately, so keep the global page/API gate out of unrelated fixtures.
    monkeypatch.setenv("GLOBAL_LOGIN_REQUIRED", "0")

    # Import lazily so the env vars above are picked up by Config.
    import importlib

    import app as app_pkg
    import app.config as config_mod

    importlib.reload(config_mod)
    importlib.reload(app_pkg)

    # Reset the (shared) PostgreSQL test database for isolation between tests,
    # then explicitly bootstrap: bootstrap re-creates anything a test dropped
    # and seeds the license/admin defaults into the now-empty tables.
    from app.bootstrap import bootstrap_app

    try:
        application = app_pkg.create_app(config_mod.Config)
        with application.app_context():
            _reset_database()
        bootstrap_app(application)
        yield application
        # Every test builds a fresh app and engine. Without disposing it the
        # idle pooled connections outlive the test, so each xdist worker holds
        # an ever-growing set and parallel runs brush max_connections (peak 81
        # of 100 at -n 8), failing an unlucky fixture with "too many clients".
        with application.app_context():
            from app.extensions import db
            db.engine.dispose()
    finally:
        # Each test gets its own instance/uploads/reports/workspace tree under
        # this directory. Without this the tree survives the run, so a suite
        # that is executed often (CI, or a mutation runner that re-runs it once
        # per mutation) quietly fills the disk -- 400+ orphans is enough to
        # start failing unrelated tests with ENOSPC.
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def client(app_ctx):
    return app_ctx.test_client()
