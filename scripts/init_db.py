#!/usr/bin/env python3
"""One-shot database initializer for the LAN Test Matrix / Silver Test Tool.

Builds a *fresh* PostgreSQL database from the authoritative SQL files and,
optionally, seeds the bootstrap administrator and license rows -- all in a
single command. It is the recommended way to stand up a new (e.g. Supabase)
database because ``db.create_all()`` alone cannot emit the ``updated_at``
trigger, table comments or the ``pgcrypto`` extension that ``sql/schema.sql``
carries.

Pipeline (each step is opt-in-safe / idempotent where noted):

    1. (optional) --drop     : DROP every project table -- destructive.
    2.            schema      : run sql/schema.sql (extensions, tables, trigger).
    3. (optional) --rls      : run sql/rls_supabase.sql (row-level security).
    4. (optional) --seed-admin: insert the bootstrap admin + license defaults.

Connection:

    The DSN is taken from --database-url or the DATABASE_URL environment
    variable (SQLAlchemy-style URLs such as
    ``postgresql+psycopg2://user:pass@host:5432/db`` are normalised for
    psycopg2 automatically).

Examples:

    # Fresh build + admin, reading DATABASE_URL from the environment / .env
    python scripts/init_db.py --seed-admin

    # Full rebuild on Supabase (pooler DSN), with RLS, wiping any old tables
    DATABASE_URL='postgresql://postgres:...@...pooler.supabase.com:6543/postgres' \
        python scripts/init_db.py --drop --yes --rls --seed-admin

This script depends only on ``psycopg2`` (already required by the app) and
``werkzeug`` (for password hashing); it does NOT import the Flask app, so it
stays usable even before the rest of the project is configured.
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


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SQL_DIR = PROJECT_DIR / "sql"
SCHEMA_FILE = SQL_DIR / "schema.sql"
RLS_FILE = SQL_DIR / "rls_supabase.sql"

# Application tables, in dependency (drop) order -- children before parents.
DROP_ORDER = [
    "lm_collab_presence",
    "lm_collab_doc",
    "lm_audit_logs",
    "lm_data_jobs",
    "lm_cell_comments",
    "lm_test_items",
    "lm_project_models",
    "lm_field_definitions",
    "lm_project_members",
    "task_events",
    "tasks",
    "lm_projects",
    "lm_users",
    "app_settings",
]

LICENSE_LIMIT_KEY = "license_limit"
LICENSE_INUSE_KEY = "license_inuse"
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "Admin@12345"

log = logging.getLogger("init_db")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalise_dsn(url: str) -> str:
    """Turn a SQLAlchemy URL into a plain libpq/psycopg2 DSN.

    ``postgresql+psycopg2://`` / ``postgresql+psycopg://`` prefixes are valid
    for SQLAlchemy but rejected by libpq, so strip the driver suffix.
    """
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://",
                   "postgres+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def resolve_dsn(cli_value: str | None) -> str:
    dsn = (cli_value or os.environ.get("DATABASE_URL", "")).strip()
    if not dsn:
        sys.exit(
            "error: no database URL. Pass --database-url or set DATABASE_URL "
            "(e.g. postgresql://user:pass@host:5432/dbname)."
        )
    return normalise_dsn(dsn)


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
    """Execute a whole .sql file in one transaction.

    psycopg2 sends the entire script to the server in a single ``execute`` call,
    which correctly handles dollar-quoted functions, DO blocks and multi-
    statement files without fragile client-side statement splitting.
    """
    if not path.is_file():
        sys.exit(f"error: SQL file not found: {path}")
    sql = path.read_text(encoding="utf-8")
    log.info("running %s (%d bytes)", path.name, len(sql))
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    log.info("  -> %s applied", path.name)


def drop_all_tables(conn) -> None:
    log.warning("dropping %d tables (CASCADE) -- destructive", len(DROP_ORDER))
    with conn.cursor() as cur:
        for table in DROP_ORDER:
            cur.execute(f'drop table if exists "{table}" cascade;')
        # Trigger function is schema-level, not table-level; remove it too so a
        # subsequent schema.sql run recreates a pristine definition.
        cur.execute("drop function if exists set_updated_at() cascade;")
    conn.commit()
    log.info("  -> existing objects dropped")


def seed_admin(conn, username: str, password: str, license_limit: int) -> None:
    """Insert the bootstrap admin and license rows if absent (idempotent)."""
    from werkzeug.security import generate_password_hash

    with conn.cursor() as cur:
        # License defaults -----------------------------------------------------
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

        # Bootstrap admin ------------------------------------------------------
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
                    not explicit,  # force change only when using built-in default
                ),
            )
            log.info(
                "  -> seeded admin '%s' (%s)",
                username,
                "password from --admin-password/env"
                if explicit else f"default password '{DEFAULT_ADMIN_PASSWORD}', "
                                 "must change on first login",
            )
    conn.commit()
    log.info("  -> license defaults ensured (limit=%d)", license_limit)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="init_db.py",
        description="One-shot fresh-database builder for the LAN Test Matrix.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--database-url", default=None,
        help="PostgreSQL DSN. Defaults to the DATABASE_URL environment variable.",
    )
    p.add_argument(
        "--schema-file", type=Path, default=SCHEMA_FILE,
        help=f"Schema DDL file (default: {SCHEMA_FILE.relative_to(PROJECT_DIR)}).",
    )
    p.add_argument(
        "--rls", action="store_true",
        help="Also apply sql/rls_supabase.sql (row-level security).",
    )
    p.add_argument(
        "--rls-file", type=Path, default=RLS_FILE,
        help=f"RLS policy file (default: {RLS_FILE.relative_to(PROJECT_DIR)}).",
    )
    p.add_argument(
        "--drop", action="store_true",
        help="DROP every project table before building. DESTRUCTIVE.",
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
        help="Initial concurrent-run license limit (default: env LICENSE_LIMIT "
             "or 4).",
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

    dsn = resolve_dsn(args.database_url)

    if args.drop and not args.yes:
        log.warning("--drop will DELETE ALL DATA in: %s", redact(dsn))
        reply = input("Type 'yes' to continue: ").strip().lower()
        if reply != "yes":
            log.info("aborted; nothing was changed")
            return 1

    log.info("connecting to %s", redact(dsn))
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.Error as exc:
        sys.exit(f"error: cannot connect: {exc}")

    try:
        conn.autocommit = False
        if args.drop:
            drop_all_tables(conn)

        run_sql_file(conn, args.schema_file)

        if args.rls:
            run_sql_file(conn, args.rls_file)

        if args.seed_admin:
            seed_admin(conn, args.admin_user, args.admin_password,
                       args.license_limit)
    except psycopg2.Error as exc:
        conn.rollback()
        sys.exit(f"error: SQL failed, rolled back: {exc}")
    finally:
        conn.close()

    log.info("database initialised successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
