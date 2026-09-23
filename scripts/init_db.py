#!/usr/bin/env python3
"""One-shot PostgreSQL initializer for the Silver Test Platform.

Application table ownership is shared with normal deployment bootstrap:

    SQLAlchemy model metadata
        + app.bootstrap.reconcile_schema()
        = current application schema

sql/schema.sql is intentionally extras-only. It contains PostgreSQL objects that
SQLAlchemy create_all() does not express (trigger function/triggers/comments).

Pipeline:

    1. (optional) --drop      drop current application tables from ORM metadata.
    2. application schema     run reconcile_schema() -- same path as bootstrap.
    3. PostgreSQL extras      run sql/schema.sql.
    4. (optional) --rls       run sql/rls_supabase.sql.
    5. (optional) --seed-admin insert bootstrap admin + license defaults.

This removes the former duplicate hand-maintained table schema and static drop
list: adding a model table automatically makes fresh initialization aware of it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

try:
    import psycopg2
except ImportError:  # pragma: no cover - clearer failure than a traceback
    sys.exit("error: psycopg2 is required (pip install psycopg2-binary)")


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SQL_DIR = PROJECT_DIR / "sql"
SCHEMA_FILE = SQL_DIR / "schema.sql"
RLS_FILE = SQL_DIR / "rls_supabase.sql"

LICENSE_LIMIT_KEY = "license_limit"
LICENSE_INUSE_KEY = "license_inuse"
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "Admin@12345"

log = logging.getLogger("init_db")


def normalise_dsn(url: str) -> str:
    """Turn a SQLAlchemy PostgreSQL URL into a libpq/psycopg2 DSN."""
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://",
                   "postgres+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def resolve_database_url(cli_value: str | None) -> str:
    url = (cli_value or os.environ.get("DATABASE_URL", "")).strip()
    if not url:
        sys.exit(
            "error: no database URL. Pass --database-url or set DATABASE_URL "
            "(e.g. postgresql://user:pass@host:5432/dbname)."
        )
    return url


def redact(dsn: str) -> str:
    """Hide the password when echoing a DSN to logs."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, _, tail = rest.partition("@")
    if ":" in creds:
        user = creds.split(":", 1)[0]
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{tail}"


def run_sql_file(conn, path: Path) -> None:
    """Execute a whole SQL file in one transaction."""
    if not path.is_file():
        sys.exit(f"error: SQL file not found: {path}")
    sql = path.read_text(encoding="utf-8")
    log.info("running %s (%d bytes)", path.name, len(sql))
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    log.info("  -> %s applied", path.name)


def drop_application_tables(conn, table_names: list[str]) -> None:
    """Drop current ORM-owned tables in dependency-safe reverse order."""
    log.warning("dropping %d application tables (CASCADE) -- destructive",
                len(table_names))
    with conn.cursor() as cur:
        for table in table_names:
            cur.execute(f'drop table if exists "{table}" cascade;')
        # schema.sql recreates this after application tables are reconciled.
        cur.execute("drop function if exists set_updated_at() cascade;")
    conn.commit()
    log.info("  -> current application tables dropped")


def seed_admin(conn, username: str, password: str, license_limit: int) -> None:
    """Insert the bootstrap admin and license rows if absent (idempotent)."""
    from werkzeug.security import generate_password_hash

    with conn.cursor() as cur:
        cur.execute(
            "insert into app_settings(key, value) values (%s, %s) "
            "on conflict (key) do nothing;",
            (LICENSE_LIMIT_KEY, str(int(license_limit))),
        )
        cur.execute(
            "insert into app_settings(key, value) values (%s, %s) "
            "on conflict (key) do nothing;",
            (LICENSE_INUSE_KEY, "0"),
        )

        cur.execute("select id from lm_users where username = %s;", (username,))
        if cur.fetchone() is not None:
            log.info("  -> admin '%s' already exists; left untouched", username)
        else:
            explicit = bool(password)
            raw = password or DEFAULT_ADMIN_PASSWORD
            cur.execute(
                "insert into lm_users "
                "(username, display_name, password_hash, status, "
                " is_system_admin, must_change_password) "
                "values (%s, %s, %s, 'active', true, %s);",
                (
                    username,
                    "System Administrator",
                    generate_password_hash(raw),
                    not explicit,
                ),
            )
            log.info(
                "  -> seeded admin '%s' (%s)",
                username,
                "password from --admin-password/env"
                if explicit else "built-in default; must change on first login",
            )
    conn.commit()
    log.info("  -> license defaults ensured (limit=%d)", license_limit)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="init_db.py",
        description="One-shot PostgreSQL initializer for the Silver Test Platform.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--database-url", default=None,
        help="PostgreSQL DSN. Defaults to the DATABASE_URL environment variable.",
    )
    p.add_argument(
        "--schema-file", type=Path, default=SCHEMA_FILE,
        help=f"PostgreSQL extras file (default: {SCHEMA_FILE.relative_to(PROJECT_DIR)}).",
    )
    p.add_argument(
        "--rls", action="store_true",
        help="Also apply sql/rls_supabase.sql (experimental row-level security).",
    )
    p.add_argument(
        "--rls-file", type=Path, default=RLS_FILE,
        help=f"RLS policy file (default: {RLS_FILE.relative_to(PROJECT_DIR)}).",
    )
    p.add_argument(
        "--drop", action="store_true",
        help="DROP every current ORM-owned application table before building.",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt required by --drop.",
    )
    p.add_argument(
        "--seed-admin", action="store_true",
        help="Insert the bootstrap admin and license default rows.",
    )
    p.add_argument(
        "--admin-user",
        default=os.environ.get("LM_ADMIN_USER", DEFAULT_ADMIN_USER),
        help="Bootstrap admin username (default: env LM_ADMIN_USER or 'admin').",
    )
    p.add_argument(
        "--admin-password",
        default=os.environ.get("LM_ADMIN_PASSWORD", ""),
        help="Bootstrap admin password (default: env LM_ADMIN_PASSWORD; empty "
             "uses the built-in default and forces a change on first login).",
    )
    p.add_argument(
        "--license-limit", type=int,
        default=int(os.environ.get("LICENSE_LIMIT", "4") or "4"),
        help="Initial concurrent-run license limit (default: env LICENSE_LIMIT or 4).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database_url = resolve_database_url(args.database_url)
    # Config is imported below, so set the selected URL before importing app.
    os.environ["DATABASE_URL"] = database_url
    dsn = normalise_dsn(database_url)

    if args.drop and not args.yes:
        log.warning("--drop will DELETE APPLICATION DATA in: %s", redact(dsn))
        reply = input("Type 'yes' to continue: ").strip().lower()
        if reply != "yes":
            log.info("aborted; nothing was changed")
            return 1

    # Import only after DATABASE_URL is final so Config/Huey bind the same DB.
    from app import create_app, models  # noqa: F401
    from app.bootstrap import reconcile_schema
    from app.extensions import db

    application = create_app()
    with application.app_context():
        # Metadata itself is the table list; no duplicate DROP_ORDER to maintain.
        table_names = [t.name for t in reversed(db.metadata.sorted_tables)]

    log.info("connecting to %s", redact(dsn))
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.Error as exc:
        sys.exit(f"error: cannot connect: {exc}")

    try:
        conn.autocommit = False
        if args.drop:
            drop_application_tables(conn, table_names)

        # Same schema path used by normal deployment bootstrap.
        with application.app_context():
            reconcile_schema()

        # PostgreSQL-only objects that SQLAlchemy metadata does not own.
        run_sql_file(conn, args.schema_file)

        if args.rls:
            run_sql_file(conn, args.rls_file)

        if args.seed_admin:
            seed_admin(conn, args.admin_user, args.admin_password,
                       args.license_limit)
    except Exception as exc:  # required initialization is fail-closed
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(f"error: database initialization failed: {exc}")
    finally:
        conn.close()

    log.info("database initialised successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
