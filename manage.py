"""Management entry point for one-owner platform bootstrap."""

from __future__ import annotations

import argparse

from app import create_app
from app.bootstrap import bootstrap_app
from app.config import Config
from app.logging_setup import configure as configure_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Silver Test Platform management")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "bootstrap",
        help="create/upgrade schema, seed defaults, and run persistent migrations",
    )
    args = parser.parse_args()

    if args.command == "bootstrap":
        configure_logging("cli", Config)
        application = create_app(Config)
        bootstrap_app(application)
        print("Bootstrap complete.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
