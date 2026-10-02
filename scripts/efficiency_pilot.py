"""Offline-first pilot reporting; explicit live mode performs a read-only audit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys

from pydantic import ValidationError

from app.services.pilot_contract import PilotInput, PilotTimings
from app.services.pilot_metrics import build_report

MAX_INPUT_BYTES = 4 * 1024 * 1024


def _unique_keys(entries):
    result = {}
    for key, value in entries:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON numbers are not allowed")


def _load_input(path: str) -> PilotInput:
    location = Path(path)
    if not location.is_file():
        raise OSError("A regular input file is required")
    with location.open("rb") as stream:
        contents = stream.read(MAX_INPUT_BYTES + 1)
    if len(contents) > MAX_INPUT_BYTES:
        raise ValueError("Pilot input exceeds the byte limit")
    payload = json.loads(contents.decode("utf-8-sig"), object_pairs_hook=_unique_keys,
                         parse_constant=_reject_constant)
    return PilotInput.model_validate(payload)


def _template() -> dict:
    modules = ["<owner-module-1>", "<owner-module-2>"]
    return {
        "schema_version": 1, "project_id": None, "modules": modules,
        "model": {"model_id": None, "version": None, "sha256": None},
        "viewpoints": [
            {"item_id": None, "version": None, "module": modules[0 if position < 10 else 1],
             "document_revision": None, "approval_reference": None}
            for position in range(20)
        ],
        "draft_ids": [], "run_attempts": [], "timings": PilotTimings().model_dump(),
    }


def _collect_live(pilot: PilotInput, actor_id: int):
    from sqlalchemy.engine import make_url

    target = os.environ.get("PILOT_DATABASE_URL", "").strip()
    if not target:
        raise ValueError("An explicit PILOT_DATABASE_URL is required")
    url = make_url(target)
    if url.drivername not in {"postgresql", "postgresql+psycopg2"}:
        raise ValueError("Only PostgreSQL with psycopg2 is supported")
    from app import create_app
    from app.config import Config
    from app.extensions import db
    from app.services.pilot_audit import collect_snapshot

    class AuditConfig(Config):
        SQLALCHEMY_DATABASE_URI = target
        SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_size": 1, "max_overflow": 0,
            "connect_args": {"connect_timeout": 5, "options": "-c default_transaction_read_only=on"},
        }
        SECRET_KEY = secrets.token_hex(32)

    application = create_app(AuditConfig)
    with application.app_context():
        try:
            return collect_snapshot(pilot, actor_id=actor_id)
        finally:
            db.session.remove()
            db.engine.dispose()


def _positive_actor(value: str) -> int:
    try:
        actor_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Actor ID must be a positive integer") from exc
    if actor_id < 1:
        raise argparse.ArgumentTypeError("Actor ID must be a positive integer")
    return actor_id


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="Owner-completed pilot JSON; never accepts trusted observations")
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument("--template", action="store_true", help="Print an unfilled input template")
    output_mode.add_argument("--schema", action="store_true", help="Print the canonical input JSON Schema")
    parser.add_argument("--live", action="store_true", help="Opt into the explicit PILOT_DATABASE_URL target")
    parser.add_argument("--actor-id", type=_positive_actor, help="Local audit actor with project-view permission")
    parser.add_argument("--mode", choices=("report", "preflight"), default="report")
    args = parser.parse_args(argv)
    if args.template or args.schema:
        if args.input or args.live or args.actor_id is not None or args.mode != "report":
            parser.error("Schema/template output cannot be combined with an audit")
        _emit(_template() if args.template else PilotInput.model_json_schema())
        return 0
    if not args.input or args.live and args.actor_id is None or not args.live and args.actor_id is not None:
        parser.error("Provide an input file; --live also requires --actor-id")
    try:
        pilot = _load_input(args.input)
    except OSError:
        print("Cannot read pilot input: provide a readable regular JSON file.", file=sys.stderr)
        return 2
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        print("Invalid pilot input: check --schema, unique identities and finite recorded timings.", file=sys.stderr)
        return 2
    snapshot = None
    if args.live:
        if not os.environ.get("PILOT_DATABASE_URL", "").strip():
            print("Live audit requires explicit PILOT_DATABASE_URL; DATABASE_URL is never a fallback.", file=sys.stderr)
            return 2
        try:
            snapshot = _collect_live(pilot, args.actor_id)
        except Exception:
            print("Live pilot audit failed: check the explicit PostgreSQL target, actor access and retained evidence.",
                  file=sys.stderr)
            return 2
    try:
        result = build_report(pilot, snapshot)
    except ValueError:
        print("Pilot observations do not match the selected session; do not reuse a different audit snapshot.",
              file=sys.stderr)
        return 2
    _emit(result)
    complete = result["readiness"]["ready"] if args.mode == "preflight" else result["measurement"]["complete"]
    return 0 if complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
