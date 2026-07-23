#!/usr/bin/env python3
"""Restore the LAN Test Matrix data from a backup (PRD §12).

The platform runs exclusively on PostgreSQL: this runs ``pg_restore --clean``
from a custom-format dump produced by ``lm_backup.py``.

Usage:
    python scripts/lm_restore.py path/to/backup

Stop the web + worker processes before restoring so no writer is active.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set — the platform requires a PostgreSQL DSN.")
    return url


def _pg_dsn(url: str) -> str:
    """Strip the SQLAlchemy ``+driver`` suffix so pg_restore gets a libpq DSN."""
    return re.sub(r"^(postgresql|postgres)\+[A-Za-z0-9_]+://", r"\1://", url)


def restore_postgres(url: str, backup: Path) -> None:
    cmd = ["pg_restore", "--clean", "--if-exists", "--no-owner",
           "--dbname", _pg_dsn(url), str(backup)]
    print("running: pg_restore --clean --if-exists --dbname <dsn>", backup)
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore LAN Test Matrix data")
    parser.add_argument("backup", help="path to a backup produced by lm_backup.py")
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args()

    backup = Path(args.backup)
    if not backup.exists():
        raise SystemExit(f"backup not found: {backup}")

    url = _database_url()
    scheme = urlparse(url).scheme
    if not args.yes:
        reply = input(f"This will OVERWRITE the current {scheme} database. Continue? [y/N] ")
        if reply.strip().lower() != "y":
            print("aborted")
            return 1

    if not scheme.startswith("postgres"):
        raise SystemExit(
            f"unsupported database scheme: {scheme} (PostgreSQL required)")
    restore_postgres(url, backup)
    print("restore complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
