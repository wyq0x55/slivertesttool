"""Run deployment commands with the same configuration as the Windows services."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from dotenv import dotenv_values


RELEASE = Path(__file__).resolve().parents[1]
ENTRYPOINTS = {
    "web": ("run_web.py",), "worker": ("run_worker.py", "--queue", "all"),
    "collab": ("run_collab.py",), "bootstrap": ("manage.py", "bootstrap"),
}


def configure(root: Path) -> None:
    env_file = root / "config/.env"
    if not env_file.is_file():
        raise SystemExit(f"Missing {env_file}")
    settings = dotenv_values(env_file, encoding="utf-8-sig")
    if not settings.get("DATABASE_URL"):
        raise SystemExit("Set DATABASE_URL explicitly in config/.env.")
    if settings.get("HUEY_DATABASE_URL") not in (None, "", settings["DATABASE_URL"]):
        raise SystemExit("Managed deployments use one database for the app and Huey.")
    secret = settings.get("SECRET_KEY") or ""
    if len(secret) < 32 or secret == "change-me-internal-secret":
        raise SystemExit("Set a private SECRET_KEY of at least 32 characters in config/.env.")
    os.environ.update({key: value for key, value in settings.items() if value is not None})
    os.environ.update({
        "HUEY_DATABASE_URL": settings["DATABASE_URL"], "HUEY_IMMEDIATE": "0",
        "LM_ALLOW_INSECURE_SECRET": "0", "PYTHON_DOTENV_DISABLED": "1",
        "INSTANCE_DIR": str(root / "data/instance"),
        "UPLOAD_DIR": str(root / "data/uploads"),
        "REPORT_DIR": str(root / "data/reports"),
        "WORKSPACE_DIR": str(root / "data/instance/workspaces"),
        "MODEL_DIR": str(root / "data/instance/model"),
        "POOL_DIR": str(root / "data/instance/pool"),
        "CURVE_CACHE_DIR": str(root / "data/instance/cache/curves"),
        "LOG_DIR": str(root / "logs"),
    })
    sys.path.insert(0, str(RELEASE))
    os.chdir(RELEASE)


def check_database(command: str) -> None:
    from sqlalchemy import create_engine, select, text

    from app.config import Config

    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI, connect_args={"connect_timeout": 5})
    with engine.connect() as connection:
        if command == "preflight":
            connection.execute(text("SELECT 1"))
            return
        if command == "empty-db":
            objects = connection.execute(text("""
                SELECT 1 FROM pg_namespace
                WHERE nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg_%'
                UNION ALL
                SELECT 1 FROM pg_class WHERE relnamespace = 'public'::regnamespace
                UNION ALL
                SELECT 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                UNION ALL
                SELECT 1 FROM pg_type WHERE typnamespace = 'public'::regnamespace
                LIMIT 1
            """)).first()
            if objects:
                raise SystemExit("Recovery requires a new empty PostgreSQL database.")
            return

        from app.models.ai_draft import AiDraft
        from app.models.lanmatrix import DataJob
        from app.models.task import Task, TaskStatus
        from app.models.ut_pipeline import UtBatch

        active = (
            (Task, (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)),
            (AiDraft, (AiDraft.STATUS_RUNNING,)),
            (DataJob, ("pending", "running")),
            (UtBatch, (UtBatch.STATUS_GENERATING, UtBatch.STATUS_RUNNING)),
        )
        for model, statuses in active:
            query = select(model.id).where(model.status.in_(statuses)).limit(1)
            if connection.execute(query).first():
                raise SystemExit(f"Active {model.__name__} records remain. Wait for background work to finish.")


def health(collab: bool) -> None:
    from app.config import Config

    def local_url(host: str, port: int, path: str) -> str:
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{host}:{port}{path}"

    expected = json.loads((RELEASE / "release.json").read_text(encoding="utf-8"))["app_version"]
    web_url = local_url(Config.HOST, Config.PORT, "/api/v1/health")
    collab_url = local_url(Config.COLLAB_HOST, Config.COLLAB_PORT, "/healthz")
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with opener.open(web_url, timeout=3) as response:
                data = json.load(response)["data"]
            if data["database"] != "ok" or data["version"] != expected:
                raise ValueError("database status or application version mismatch")
            if collab:
                with opener.open(collab_url, timeout=3) as response:
                    if response.read() != b"collab server up (websocket only)\n":
                        raise ValueError("unexpected collaboration health response")
        except (URLError, OSError, ValueError, KeyError, TypeError) as error:
            last_error = error
            time.sleep(1)
        else:
            print(f"Web and database ready for application version {expected}.")
            return
    raise SystemExit(f"Health check failed at {web_url}: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("command", choices=[*ENTRYPOINTS, "preflight", "idle", "empty-db", "backup", "restore", "health"])
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--collab", action="store_true")
    args = parser.parse_args()
    if (args.command in ("backup", "restore")) != (args.path is not None):
        parser.error("A path is required for backup and restore only.")
    configure(args.root.resolve())
    if args.command in ENTRYPOINTS:
        script, *argv = ENTRYPOINTS[args.command]
        sys.argv = [script, *argv]
        runpy.run_path(script, run_name="__main__")
    elif args.command in ("preflight", "idle", "empty-db"):
        check_database(args.command)
    elif args.command == "backup":
        from scripts.lm_backup import backup_postgres

        dump = backup_postgres(os.environ["DATABASE_URL"], args.path)
        dump.rename(args.path / "database.dump")
    elif args.command == "restore":
        from scripts.lm_backup import _pg_dsn

        check_database("empty-db")
        subprocess.run(["pg_restore", "--single-transaction", "--no-owner", "--no-acl",
                        "--dbname", _pg_dsn(os.environ["DATABASE_URL"]), str(args.path)], check=True)
    else:
        health(args.collab)


if __name__ == "__main__":
    main()
