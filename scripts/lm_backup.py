#!/usr/bin/env python3
"""Backup the LAN Test Matrix data (PRD §12).

The platform runs exclusively on PostgreSQL: this uses ``pg_dump`` (custom,
compressed, restorable format) against ``DATABASE_URL``.

Usage:
    python scripts/lm_backup.py [--out-dir backups] [--keep 30]

The retention flag prunes older backups so a nightly cron stays bounded. Run
offline on the LAN host.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set — the platform requires a PostgreSQL DSN.")
    return url


def _pg_dsn(url: str) -> str:
    """Strip the SQLAlchemy ``+driver`` suffix so pg_dump gets a libpq DSN."""
    return re.sub(r"^(postgresql|postgres)\+[A-Za-z0-9_]+://", r"\1://", url)


def backup_postgres(url: str, out_dir: Path) -> Path:
    target = out_dir / f"lanmatrix_pg_{_timestamp()}.dump"
    # pg_dump reads the DSN directly; --format=custom = compressed, restorable.
    cmd = ["pg_dump", "--format=custom", "--file", str(target), _pg_dsn(url)]
    print("running: pg_dump --format=custom --file", target, "<dsn>")
    subprocess.run(cmd, check=True)
    return target


def prune(out_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(out_dir.glob("lanmatrix_*"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
        print("pruned", old.name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup LAN Test Matrix data")
    parser.add_argument("--out-dir", default="backups")
    parser.add_argument("--keep", type=int, default=30, help="retain N newest backups")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    url = _database_url()
    scheme = urlparse(url).scheme
    if not scheme.startswith("postgres"):
        raise SystemExit(
            f"unsupported database scheme: {scheme} (PostgreSQL required)")
    target = backup_postgres(url, out_dir)

    prune(out_dir, args.keep)
    print("backup written:", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
