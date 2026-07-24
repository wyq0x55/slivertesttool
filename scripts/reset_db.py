#!/usr/bin/env python3
"""Destroy and rebuild the whole database from scratch -- one command.

This is the "nuke it and start over" tool. Unlike ``init_db.py --drop`` (which
drops a known list of tables), ``reset_db.py`` wipes the **entire** ``public``
schema -- every table, view, sequence, function, type and any stray leftover
object -- then rebuilds from the authoritative SQL. Use it when you want a
guaranteed-pristine database and do not care about the existing data.

    DROP SCHEMA public CASCADE  ->  CREATE SCHEMA public  ->  regrant privileges
      ->  sql/schema.sql  ->  (optional) sql/rls_supabase.sql  ->  (optional) seed

It reuses ``init_db.py`` for DSN handling, SQL execution and admin seeding, so
the two scripts never drift apart. Depends only on ``psycopg2`` + ``werkzeug``;
it does NOT import the Flask app.

Examples:

    # Full wipe + rebuild + bootstrap admin (prompts before destroying)
    python scripts/reset_db.py --seed-admin

    # Non-interactive rebuild on Supabase, with RLS (for CI / scripted resets)
    DATABASE_URL='postgresql://postgres:...@...pooler.supabase.com:6543/postgres' \
        python scripts/reset_db.py --yes --rls --seed-admin

WARNING: this DELETES ALL DATA in the target database's public schema and cannot
be undone. It refuses to run without --yes unless you confirm at the prompt.
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

# Reuse everything from init_db so the two scripts stay in lock-step.
import init_db

log = logging.getLogger("reset_db")

# Supabase-managed roles we re-grant to *if they exist*. On a vanilla PostgreSQL
# these are simply absent and skipped, so the same script works on both.
_MANAGED_ROLES = ("anon", "authenticated", "service_role")


def reset_schema(conn) -> None:
    """Drop and recreate the public schema, restoring baseline privileges.

    ``DROP SCHEMA public CASCADE`` removes *all* objects unconditionally, which
    is more thorough (and more robust to unknown leftovers) than dropping a
    fixed table list. Afterwards we restore the default grants so both a plain
    PostgreSQL and a Supabase project keep working: the current role and PUBLIC
    always, plus Supabase's anon/authenticated/service_role when present.
    """
    log.warning("dropping the ENTIRE public schema (all objects) -- destructive")
    with conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade;")
        cur.execute("create schema public;")

        # Baseline privileges (safe everywhere).
        cur.execute("grant all on schema public to current_user;")
        cur.execute("grant usage on schema public to public;")

        # Supabase roles -- grant only the ones that actually exist.
        for role in _MANAGED_ROLES:
            cur.execute("select 1 from pg_roles where rolname = %s;", (role,))
            if cur.fetchone() is not None:
                # Role names here are a fixed allow-list, not user input.
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
        description="Wipe the whole public schema and rebuild from sql/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--database-url", default=None,
        help="PostgreSQL DSN. Defaults to the DATABASE_URL environment variable.",
    )
    p.add_argument(
        "--schema-file", type=Path, default=init_db.SCHEMA_FILE,
        help="Schema DDL file (default: sql/schema.sql).",
    )
    p.add_argument(
        "--rls", action="store_true",
        help="Also apply sql/rls_supabase.sql after the schema.",
    )
    p.add_argument(
        "--rls-file", type=Path, default=init_db.RLS_FILE,
        help="RLS policy file (default: sql/rls_supabase.sql).",
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
        help="Initial concurrent-run license limit (default: env LICENSE_LIMIT "
             "or 4).",
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

    dsn = init_db.resolve_dsn(args.database_url)
    safe = init_db.redact(dsn)

    if not args.yes:
        log.warning("this will PERMANENTLY DELETE the public schema in: %s", safe)
        reply = input("Type 'RESET' to confirm: ").strip()
        if reply != "RESET":
            log.info("aborted; nothing was changed")
            return 1

    log.info("connecting to %s", safe)
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.Error as exc:
        sys.exit(f"error: cannot connect: {exc}")

    try:
        conn.autocommit = False
        reset_schema(conn)
        init_db.run_sql_file(conn, args.schema_file)
        if args.rls:
            init_db.run_sql_file(conn, args.rls_file)
        if args.seed_admin:
            init_db.seed_admin(conn, args.admin_user, args.admin_password,
                               args.license_limit)
    except psycopg2.Error as exc:
        conn.rollback()
        sys.exit(f"error: SQL failed, rolled back: {exc}")
    finally:
        conn.close()

    log.info("database reset and rebuilt successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
