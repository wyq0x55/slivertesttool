#!/usr/bin/env python3
"""Generate or verify requirements.txt from the frozen uv lock.

pyproject.toml is the only hand-maintained direct-dependency source.
uv.lock is the resolved lock; requirements.txt is only the pip/offline export.
"""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
HEADER = (
    "# Generated from pyproject.toml + uv.lock by scripts/export_requirements.py.\n"
    "# Do not edit by hand. Regenerate with: python scripts/export_requirements.py\n"
    "# Includes all dependency groups to preserve the existing dev/offline workflow.\n"
)
EXPORT_ARGS = (
    "export",
    "--format",
    "requirements.txt",
    "--frozen",
    "--all-groups",
    "--no-hashes",
    "--no-emit-local",
    "--no-header",
)


def _run(*args: str) -> None:
    subprocess.run(("uv", *args), cwd=ROOT, check=True)


def render() -> str:
    # Fail before export if pyproject.toml and uv.lock disagree.
    _run("lock", "--check")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "requirements.txt"
        _run(*EXPORT_ARGS, "--output-file", str(out))
        body = out.read_text(encoding="utf-8").replace("\r\n", "\n")
    return HEADER + body.rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if requirements.txt differs from the frozen lock export",
    )
    args = parser.parse_args()

    generated = render()
    if args.check:
        current = (
            REQUIREMENTS.read_text(encoding="utf-8").replace("\r\n", "\n")
            if REQUIREMENTS.exists()
            else ""
        )
        if current == generated:
            return 0
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile="requirements.txt",
                tofile="requirements.txt (generated)",
            )
        )
        return 1

    REQUIREMENTS.write_text(generated, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
