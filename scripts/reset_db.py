#!/usr/bin/env python3
"""Destroy and rebuild the whole PostgreSQL public schema.

This is the destructive pristine-reset tool. It wipes the entire public schema,
recreates baseline privileges, then rebuilds through the same schema ownership
path as normal deployment:

    DROP SCHEMA public CASCADE
      -> CREATE SCHEMA public
      -> app.bootstrap.reconcile_schema()
      -> sql/schema.sql PostgreSQL extras
      -> optional seed

RLS is intentionally not part of this supported runtime path. The repository
keeps sql/rls_supabase.sql only as an experimental/reference artifact because
the Flask application does not currently bind request identity into PostgreSQL
RLS context.

WARNING: this deletes all data in the target database's public schema.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

try:
    import psycopg2
except ImportError:  # pragma: no cover
    sys.exit("error: psycopg2 is required (pip install psycopg2-binary)")

import init_db

log = logging.getLogger("reset_db")

# Hosted PostgreSQL providers may pre-create these roles. Restore schema usage
# when they exist; this does not enable or configure RLS.
_MANAGED_ROLES = ("anon", "authenticated", "service_role")


def reset_schema(conn) -> None:
    """Drop/recreate public and restore baseline schema privileges."""
    log.warning("dropping the ENTIRE public schema (all objects) -- destructive")
    with conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade;")
        cur.execute("create schema public;")
        cur.execute("grant all on schema public to current_user;")
        cur.execute("grant usage on schema public to public;")

        for role in _MANAGED_ROLES:
            cur.execute("select 1 from pg_roles where rolname = %s;", (role,))
            if cur.fetchone() is not None:
                cur.execute(f"grant usage on schema public to {role};")
                cur.execute(
                    f"alter default privileges in schema public "
                    f"grant all on tables to {role};"
                )
                log.info("  -> restored grants for managed role '%s'", role)
    conn.commit()
    log.info("  -> public schema recreated (pristine)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="reset_db.py",
        description="Wipe public and rebuild through the application schema owner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--database-url", default=None,
        help="PostgreSQL DSN. Defaults to the DATABASE_URL environment variable.",
    )
    p.add_argument(
        "--schema-file", type=Path, default=init_db.SCHEMA_FILE,
        help="PostgreSQL extras file (default: sql/schema.sql).",
    )
    p.add_argument(
        "--seed-admin", action="store_true",
        help="Insert the bootstrap admin and license default rows.",
    )
    p.add_argument(
        "--admin-user",
        default=os.environ.get("LM_ADMIN_USER", init_db.DEFAULT_ADMIN_USER),
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
        "--yes", "-y", action="store_true",
        help="Skip the destructive-action confirmation prompt.",
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

    database_url = init_db.resolve_database_url(args.database_url)
    os.environ["DATABASE_URL"] = database_url
    dsn = init_db.normalise_dsn(database_url)
    safe = init_db.redact(dsn)

    if not args.yes:
        log.warning("this will PERMANENTLY DELETE the public schema in: %s", safe)
        reply = input("Type 'RESET' to confirm: ").strip()
        if reply != "RESET":
            log.info("aborted; nothing was changed")
            return 1

    # Import only after DATABASE_URL is final.
    from app import create_app
    from app.bootstrap import reconcile_schema

    application = create_app()

    log.info("connecting to %s", safe)
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.Error as exc:
        sys.exit(f"error: cannot connect: {exc}")

    try:
        conn.autocommit = False
        reset_schema(conn)

        with application.app_context():
            reconcile_schema()

        init_db.run_sql_file(conn, args.schema_file)

        if args.seed_admin:
            init_db.seed_admin(conn, args.admin_user, args.admin_password,
                               args.license_limit)
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(f"error: database reset failed: {exc}")
    finally:
        conn.close()

    log.info("database reset and rebuilt successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
