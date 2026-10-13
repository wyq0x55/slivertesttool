"""Local performance baseline for the two candidate clocks.

This is not a CI gate and not an efficiency claim. ``--assets`` measures the
editor's Univer payload. ``--live`` times dashboard reads and the editor's
paged row load on a disposable PostgreSQL schema, then drops that schema.
It never bootstraps, migrates, or writes ``public`` tables.

Set ``PERF_DATABASE_URL`` to an explicit PostgreSQL URL before ``--live``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVER_JS = REPO_ROOT / "app" / "static" / "vendor" / "univer" / "univer.full.umd.js"
UNIVER_CSS = REPO_ROOT / "app" / "static" / "vendor" / "univer" / "univer.full.umd.css"
# editor.js fetches the whole sheet in pages of this size.
EDITOR_PAGE_SIZE = 500
ITEM_COUNT = 2000
RUN_COUNT = 20000
SCHEMA_RE = re.compile(r"^perf_[0-9a-f]{8}$")
REFUSED_DATABASES = {"postgres", "template0", "template1"}
_ROUTING_KEYS = {"host", "hostaddr", "dbname", "service", "servicefile"}


class BaselineError(RuntimeError):
    """The baseline cannot run safely."""


def percentile(samples: list[float], pct: float) -> float:
    """Linear percentile. ``pct`` is 0..1."""
    if not samples:
        raise ValueError("percentile requires samples")
    if not 0 <= pct <= 1:
        raise ValueError("percentile must be between 0 and 1")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def new_schema_name() -> str:
    return "perf_" + secrets.token_hex(4)


def validate_database_url(target: str) -> str:
    """Reject anything that is not an explicit PostgreSQL database URL."""
    from sqlalchemy.engine import make_url

    raw = (target or "").strip()
    if not raw:
        raise BaselineError("PERF_DATABASE_URL is required for --live")
    url = make_url(raw)
    query_keys = {key for key, _value in parse_qsl(urlsplit(raw).query, keep_blank_values=True)}
    database = (url.database or "").strip()
    if url.drivername not in {"postgresql", "postgresql+psycopg2"}:
        raise BaselineError("Only PostgreSQL with psycopg2 is supported")
    if (not url.host or not url.host.strip() or not database
            or _ROUTING_KEYS & query_keys):
        raise BaselineError(
            "An explicit PostgreSQL host and database, without routing overrides, is required")
    if database in REFUSED_DATABASES:
        raise BaselineError("Refusing a maintenance database")
    return raw


def _gzip_size(payload: bytes) -> int:
    return len(gzip.compress(payload, compresslevel=6, mtime=0))


def _node_parse_ms(path: Path) -> float | None:
    script = (
        "const fs=require('fs');"
        "const source=fs.readFileSync(process.argv[1]);"
        "const start=process.hrtime.bigint();"
        "new Function(source.toString('utf8'));"
        "const ms=Number(process.hrtime.bigint()-start)/1e6;"
        "process.stdout.write(String(ms));"
    )
    try:
        completed = subprocess.run(
            ["node", "-e", script, str(path)],
            check=False,
            capture_output=True,
            timeout=90,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def asset_report(js_path: Path = UNIVER_JS, css_path: Path = UNIVER_CSS,
                 *, parse_javascript: bool = True) -> dict:
    """Byte sizes, plus a Node parse time that is not browser LCP."""
    report = {}
    for label, path, parse in (
        ("univer_js", js_path, parse_javascript),
        ("univer_css", css_path, False),
    ):
        payload = path.read_bytes()
        entry = {"path": str(path), "bytes": len(payload), "gzip_bytes": _gzip_size(payload)}
        if parse:
            entry["node_parse_ms"] = _node_parse_ms(path)
        report[label] = entry
    return report


def _summary(samples: list[float]) -> dict:
    rounded = [round(value, 1) for value in samples]
    return {
        "p50_ms": round(percentile(samples, 0.5), 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        "samples_ms": rounded,
    }


def _time_call(fn, repeats: int) -> dict:
    fn()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return _summary(samples)


def _public_project_count(connection) -> int | None:
    from sqlalchemy import text

    exists = connection.execute(text("SELECT to_regclass('public.lm_projects')")).scalar()
    if exists is None:
        return None
    return int(connection.execute(text("SELECT count(*) FROM public.lm_projects")).scalar())


def _assert_isolated(connection, schema: str, before_count: int | None) -> None:
    from sqlalchemy import text

    current = connection.execute(text("SELECT current_schema()")).scalar()
    if current != schema:
        raise BaselineError("Refusing to write outside the disposable schema")
    located = connection.execute(
        text(
            "SELECT n.nspname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = 'lm_projects' AND n.nspname = :schema"
        ),
        {"schema": schema},
    ).scalar()
    if located != schema:
        raise BaselineError("Disposable project table was not created in the perf schema")
    after_count = _public_project_count(connection)
    if after_count != before_count:
        raise BaselineError("public.lm_projects changed; baseline aborted")


def _seed(session) -> None:
    from datetime import datetime, timedelta, timezone

    from app.models import Project, TestItemRow, TestRunRecord

    now = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    session.add(Project(code="perfbase", name="Performance baseline", status="active"))
    session.flush()
    project = session.query(Project).filter_by(code="perfbase").one()
    items = []
    for index in range(ITEM_COUNT):
        items.append({
            "uuid": f"{index:032x}",
            "project_id": project.id,
            "row_order": index,
            "sheet": "test",
            "case_id": f"TC{index:05d}",
            "title": f"baseline {index}",
            "result": "pass" if index % 2 == 0 else "fail",
            "workflow_status": "Draft",
            "custom_values": {"item_created": ""},
            "precondition": "",
            "test_steps": "",
            "expected_result": "",
            "actual_result": "",
            "comment": "",
            "review_status": "",
            "review_note": "",
            "review_verdict": "",
            "exempt_status": "",
            "exempt_value": "",
            "exempt_note": "",
            "version": 1,
            "created_at": now,
            "updated_at": now,
        })
    for start in range(0, len(items), 500):
        session.execute(TestItemRow.__table__.insert(), items[start:start + 500])
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    runs = []
    for index in range(ITEM_COUNT):
        for run_index in range(RUN_COUNT // ITEM_COUNT):
            moment = base + timedelta(days=(index + run_index) % 60, minutes=run_index)
            runs.append({
                "project_id": project.id,
                "row_uuid": f"{index:032x}",
                "test_id": f"TC{index:05d}",
                "task_key": "TPERF",
                "run_count": run_index + 1,
                "verdict": "pass" if run_index % 2 == 0 else "fail",
                "outcome": "pass" if run_index % 2 == 0 else "fail",
                "model_name": "PerfModel",
                "model_version": "1.0" if run_index < 5 else "1.1",
                "executor_name": "baseline",
                "executed_at": moment.replace(tzinfo=None),
                "executed_on": moment.strftime("%Y-%m-%d"),
            })
    for start in range(0, len(runs), 2000):
        session.execute(TestRunRecord.__table__.insert(), runs[start:start + 2000])


def measure_live(target: str, repeats: int) -> dict:
    """Time the dashboard and editor read paths, then drop the schema."""
    import psycopg2
    from sqlalchemy import event, text
    from sqlalchemy.engine import make_url

    from app import create_app
    from app.config import Config
    from app.extensions import db

    validate_database_url(target)
    url = make_url(target)
    schema = new_schema_name()
    if not SCHEMA_RE.fullmatch(schema):
        raise BaselineError("Generated schema name was rejected")
    admin = psycopg2.connect(
        dbname=url.database, user=url.username, password=url.password,
        host=url.host, port=url.port or 5432,
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {schema}")
    except Exception:
        admin.close()
        raise

    class PerfConfig(Config):
        SQLALCHEMY_DATABASE_URI = target

    app = create_app(PerfConfig)

    def _search_path(_dialect, _record, _arguments, parameters):
        options = parameters.get("options") or ""
        parameters["options"] = f"{options} -c search_path={schema}".strip()

    try:
        with app.app_context():
            for engine in db.engines.values():
                if engine.dialect.name == "postgresql":
                    event.listen(engine, "do_connect", _search_path)
            db.engine.dispose()
            before_count = _public_project_count(db.session.connection())
            db.session.rollback()
            import app.models  # noqa: F401
            db.create_all()
            _assert_isolated(db.session.connection(), schema, before_count)
            _seed(db.session)
            db.session.commit()
            _assert_isolated(db.session.connection(), schema, before_count)
            db.session.execute(text(f"ANALYZE {schema}.lm_test_items"))
            db.session.execute(text(f"ANALYZE {schema}.lm_test_run_records"))
            db.session.commit()

            from app.models import Project
            from app.services.lanmatrix import items_service
            from app.services.lanmatrix.dashboard_service import (
                by_version, compare_versions, snapshot, trend,
            )

            project = db.session.query(Project).filter_by(code="perfbase").one()
            project_id = project.id

            def _editor_load():
                seen = 0
                page = 1
                total = None
                while total is None or seen < total:
                    batch = items_service.list_items(
                        project_id, page=page, page_size=EDITOR_PAGE_SIZE, sheet="test")
                    total = batch["total"]
                    seen += len(batch["items"])
                    if not batch["items"]:
                        break
                    page += 1
                if seen != ITEM_COUNT:
                    raise BaselineError("Editor load did not see the seeded sheet")

            report = {
                "host": url.host,
                "database": url.database,
                "schema": schema,
                "dataset": {
                    "items": ITEM_COUNT,
                    "run_records": RUN_COUNT,
                    "editor_page_size": EDITOR_PAGE_SIZE,
                },
                "repeats": repeats,
                "dashboard_ms": {
                    "snapshot": _time_call(lambda: snapshot(project), repeats),
                    "trend": _time_call(lambda: trend(project_id), repeats),
                    "by_version": _time_call(lambda: by_version(project_id), repeats),
                    "compare_versions": _time_call(
                        lambda: compare_versions(project_id, "PerfModel@1.0", "PerfModel@1.1"),
                        repeats,
                    ),
                    "editor_sheet_load": _time_call(_editor_load, repeats),
                },
            }
            _assert_isolated(db.session.connection(), schema, before_count)
            return report
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure the local performance baseline")
    parser.add_argument("--assets", action="store_true", help="Measure the Univer payload")
    parser.add_argument("--live", action="store_true", help="Measure dashboard reads")
    parser.add_argument("--repeats", type=int, default=5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeats < 1 or args.repeats > 20:
        print("repeats must be from 1 to 20", file=sys.stderr)
        return 2
    run_assets = args.assets or not args.live
    report = {
        "schema_version": 1,
        "ci_gate": False,
        "efficiency_claim": False,
        "notes": [
            "Node parse time is not browser LCP.",
            "Dashboard timings are one local PostgreSQL sample, not a rollout gate.",
        ],
    }
    try:
        if run_assets:
            report["editor_assets"] = asset_report()
        if args.live:
            report["live"] = measure_live(
                os.environ.get("PERF_DATABASE_URL", ""), args.repeats)
    except BaselineError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    print(text)
    if args.live:
        destination = REPO_ROOT / ".cache" / "perf" / "baseline.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
