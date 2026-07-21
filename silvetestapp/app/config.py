"""Application configuration.

Values are read from environment variables (optionally loaded from a ``.env``
file via python-dotenv) with safe defaults so the platform starts out of the
box on an internal network. Configuration is intentionally centralised here so
both the web process (``run_web.py``) and the Huey worker (``run_worker.py``)
share one source of truth.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # optional; harmless if python-dotenv is absent
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# Project root = directory that contains the ``app`` package.
BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    """Flask + platform configuration."""

    # --- Flask ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-internal-secret")
    # Session cookie hardening (LAN Test Matrix auth). HttpOnly blocks JS access;
    # SameSite=Lax mitigates CSRF; Secure stays off for plain-HTTP LAN unless a
    # TLS terminator is in front (set SESSION_COOKIE_SECURE=1 then).
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _as_bool(os.environ.get("SESSION_COOKIE_SECURE"), False)
    import datetime as _dt
    PERMANENT_SESSION_LIFETIME = _dt.timedelta(
        hours=_as_int(os.environ.get("SESSION_HOURS"), 12)
    )

    # --- Storage roots ---
    INSTANCE_DIR = Path(os.environ.get("INSTANCE_DIR", BASE_DIR / "instance"))
    UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads"))
    REPORT_DIR = Path(os.environ.get("REPORT_DIR", BASE_DIR / "reports"))
    WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", BASE_DIR / "instance" / "workspaces"))
    # Where the admin-configured ``.sil`` plant model is stored (uploaded once
    # via the admin page and shared by every test run).
    MODEL_DIR = Path(os.environ.get("MODEL_DIR", BASE_DIR / "instance" / "model"))

    # --- Database (PostgreSQL only) ---
    # The platform runs exclusively on PostgreSQL (the former bundled-SQLite
    # option has been removed). ``DATABASE_URL`` is a standard SQLAlchemy DSN;
    # the LAN-friendly default targets a local PostgreSQL so a single-node pilot
    # works out of the box once the server is provisioned.
    #
    #   DATABASE_URL=postgresql+psycopg2://user:pass@dbhost:5432/silvetestapp
    #
    DEFAULT_DATABASE_URL = (
        "postgresql+psycopg2://postgres:postgres@localhost:5432/silvetestapp"
    )
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get("DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # PostgreSQL connection pool tuned for the web + worker processes sharing one
    # LAN database server. ``pool_pre_ping`` transparently drops connections that
    # died across a network blip or a server restart.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": 10,
        "max_overflow": 20,
    }

    # --- Huey task queue (PostgreSQL-backed via peewee/SqlHuey) ---
    # The queue lives in the same PostgreSQL server as the application data
    # (huey's own tables), so no separate broker (Redis/RabbitMQ) and no local
    # SQLite file are required. By default it reuses ``DATABASE_URL``; set
    # ``HUEY_DATABASE_URL`` to point the queue at a different database.
    HUEY_DATABASE_URL = (
        os.environ.get("HUEY_DATABASE_URL", "").strip() or SQLALCHEMY_DATABASE_URI
    )
    # Name of the huey queue (also used as the table prefix in PostgreSQL).
    HUEY_NAME = os.environ.get("HUEY_NAME", "silvetestapp")
    # Worker process concurrency. Keep this comfortably above the license
    # limit; the effective number of concurrent Silver runs is enforced by the
    # runtime-adjustable DB license gate, not by this pool size.
    HUEY_WORKERS = _as_int(os.environ.get("HUEY_WORKERS"), 16)

    # --- License / concurrency ---
    # Startup default for the maximum number of concurrent Silver executions.
    # May be changed at runtime from the admin page; the effective value is then
    # persisted in the database and applied live (no worker restart needed).
    LICENSE_LIMIT = _as_int(os.environ.get("LICENSE_LIMIT"), 4)

    # --- Silver runner ---
    # "silver" -> real Synopsys Silver backend (requires SILVER_HOME).
    # "mock"   -> deterministic simulation (demo / CI / no license).
    RUNNER_BACKEND = os.environ.get("RUNNER_BACKEND", "mock")
    DEFAULT_SIL_RELPATH = os.environ.get("DEFAULT_SIL_RELPATH", "model.sil")
    EXECUTION_TIMEOUT = _as_int(os.environ.get("EXECUTION_TIMEOUT"), 5000)
    SILVER_GUI = _as_bool(os.environ.get("SILVER_GUI"), False)

    # --- Pre-warmed Silver instance pool ---
    # When enabled, the worker launches ``license_limit`` empty Silver instances
    # at start-up and reuses them for every test (see app.runners.silver_pool),
    # removing the per-test Silver launch cost and pre-empting the licenses. Set
    # SILVER_POOL_ENABLED=0 to fall back to launching a dedicated instance per
    # test (the classic behaviour).
    SILVER_POOL_ENABLED = _as_bool(os.environ.get("SILVER_POOL_ENABLED"), True)
    # Whether the worker eagerly warms the pool on start-up (vs. lazily on first
    # demand). Eager warming front-loads the launch cost and grabs the licenses
    # immediately.
    SILVER_POOL_PREWARM = _as_bool(os.environ.get("SILVER_POOL_PREWARM"), True)
    # How often (seconds) the worker reconciles the pool size with the runtime
    # license limit (which an admin may change live).
    SILVER_POOL_RECONCILE_SECONDS = float(
        os.environ.get("SILVER_POOL_RECONCILE_SECONDS", "5")
    )
    # Directory where each pooled instance keeps its stable console-log file.
    POOL_DIR = Path(os.environ.get("POOL_DIR", BASE_DIR / "instance" / "pool"))

    # --- Silver shutdown cleanup ---
    # On process exit the worker disposes every pooled instance to release its
    # license. Because a hard/abrupt shutdown (e.g. the parent launcher killing
    # the worker on Windows) can skip the graceful dispose and leave orphaned
    # Silver processes holding licenses, we additionally sweep and force-kill any
    # remaining Silver processes when the app stops. Set SILVER_KILL_ON_EXIT=0 to
    # disable this safety net (e.g. if you run other Silver instances alongside).
    SILVER_KILL_ON_EXIT = _as_bool(os.environ.get("SILVER_KILL_ON_EXIT"), True)
    # Image names swept by the exit cleanup (comma-separated). Defaults cover the
    # common Synopsys Silver executables on Windows.
    SILVER_PROCESS_IMAGE_NAMES = [
        n.strip() for n in os.environ.get(
            "SILVER_PROCESS_IMAGE_NAMES", "silver.exe,silver64.exe,SilverSim.exe"
        ).split(",") if n.strip()
    ]

    # --- Upload limits ---
    MAX_UPLOAD_BYTES = _as_int(
        os.environ.get("MAX_UPLOAD_BYTES"), 512 * 1024 * 1024  # 512 MiB
    )
    MAX_CONTENT_LENGTH = MAX_UPLOAD_BYTES

    # --- Web server ---
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = _as_int(os.environ.get("PORT"), 8080)

    # --- Admin ---
    # Administration is authorised solely by the LAN Test Matrix system-admin
    # session (RBAC); the former shared ``ADMIN_TOKEN`` has been removed because
    # its default value allowed any logged-in user to escalate to admin. Grant a
    # user "System Administrator" (see LM_ADMIN_* below) to authorise the admin
    # console and the ``/api/admin/*`` + ``/api/v1/admin/*`` endpoints.

    # --- LAN Test Matrix (online editor) ------------------------------------
    # The formerly self-contained ``lanmatrix`` project is now expanded into the
    # platform service layer (``app.services.lanmatrix``). Every knob it used to
    # hard-code lives here so it is driven by the same ``.env`` as the rest of
    # the platform. The pure service modules read these through the thin
    # :mod:`app.services.lanmatrix.settings` indirection.

    # Bootstrap system administrator, seeded on first start when no system admin
    # exists yet. When LM_ADMIN_PASSWORD is left empty the built-in default is
    # used *and* the admin is forced to change it on first login; setting it
    # explicitly disables that forced change.
    LM_ADMIN_USER = os.environ.get("LM_ADMIN_USER", "admin")
    LM_ADMIN_PASSWORD = os.environ.get("LM_ADMIN_PASSWORD", "")
    LM_ADMIN_DEFAULT_PASSWORD = "Admin@12345"

    # Account lockout: lock an account for LM_LOCK_MINUTES after
    # LM_LOCK_THRESHOLD consecutive failed logins.
    LM_LOCK_THRESHOLD = _as_int(os.environ.get("LM_LOCK_THRESHOLD"), 5)
    LM_LOCK_MINUTES = _as_int(os.environ.get("LM_LOCK_MINUTES"), 15)

    # Server-side pagination for item / audit listings (default and hard cap).
    LM_PAGE_SIZE = _as_int(os.environ.get("LM_PAGE_SIZE"), 100)
    LM_PAGE_SIZE_MAX = _as_int(os.environ.get("LM_PAGE_SIZE_MAX"), 500)

    # Max sample rows returned by a batch search/replace preview.
    LM_BATCH_SAMPLE_LIMIT = _as_int(os.environ.get("LM_BATCH_SAMPLE_LIMIT"), 100)
    # Max import-validation errors echoed back to the client.
    LM_IMPORT_ERROR_LIMIT = _as_int(os.environ.get("LM_IMPORT_ERROR_LIMIT"), 500)

    # User-supplied regex guards (search/replace + field ``pattern`` validation):
    # a hard length cap and a per-match wall-clock timeout (seconds) to bound
    # catastrophic backtracking.
    LM_REGEX_MAX_LEN = _as_int(os.environ.get("LM_REGEX_MAX_LEN"), 200)
    LM_REGEX_TIMEOUT = float(os.environ.get("LM_REGEX_TIMEOUT", "0.25"))

    # Max length of a sanitised uploaded / exported filename base.
    LM_FILENAME_MAX_LEN = _as_int(os.environ.get("LM_FILENAME_MAX_LEN"), 120)

    # Test-Matrix (Excel) bridge defaults: the ID column prefix and the summary
    # sheet name of the round-tripped workbook.
    LM_TM_ID_PREFIX = os.environ.get("LM_TM_ID_PREFIX", "ID;;")
    LM_TM_SUMMARY_SHEET = os.environ.get("LM_TM_SUMMARY_SHEET", "4.TestRequirement")

    # --- Self-service registration (LAN users) ------------------------------
    # Lets users on the internal network create their own account from the login
    # page. A freshly registered account carries no project membership and is
    # not a system administrator, so it can log in but sees nothing until an
    # administrator grants it a project role — a safe default for a LAN tool.
    LM_ALLOW_REGISTRATION = _as_bool(os.environ.get("LM_ALLOW_REGISTRATION"), True)
    # Status assigned to a new self-registered account:
    #   "active"   -> can log in immediately (default, best for trusted LANs);
    #   "disabled" -> created in a pending state; an admin must activate it.
    LM_REGISTRATION_DEFAULT_STATUS = (
        os.environ.get("LM_REGISTRATION_DEFAULT_STATUS", "active").strip().lower()
    )
    # Minimum length enforced for a registration / self-set password.
    LM_PASSWORD_MIN_LEN = _as_int(os.environ.get("LM_PASSWORD_MIN_LEN"), 8)
    # Whitelist pattern a chosen username must fully match.
    LM_USERNAME_PATTERN = os.environ.get(
        "LM_USERNAME_PATTERN", r"^[A-Za-z0-9_.\-]{3,64}$"
    )

    # --- Unified authentication ---
    # When enabled, the *entire* site (the original upload/execute UI, the Test
    # Matrix pages and their APIs) is gated behind the Matrix Editor login. Set
    # GLOBAL_LOGIN_REQUIRED=0 to restore the legacy open behaviour.
    GLOBAL_LOGIN_REQUIRED = str(
        os.environ.get("GLOBAL_LOGIN_REQUIRED", "1")
    ).strip().lower() not in ("0", "false", "no", "off")

    # --- Real-time collaboration (Yjs/CRDT) ---
    # Explicit WebSocket base for the collab server (e.g. "wss://host:1234").
    # When empty the frontend derives it from window.location.
    COLLAB_WS_URL = os.environ.get("COLLAB_WS_URL", "").strip()
    # Room lifecycle: the collab server keeps one in-memory room (Y.Doc +
    # Materializer) per open project. To bound memory on a long-running server,
    # a background sweeper evicts rooms that have had no connected client for
    # COLLAB_ROOM_IDLE_TTL_SECONDS (their state stays durable in PgYStore and is
    # rehydrated on reconnect). Set the TTL to 0 to disable eviction.
    COLLAB_ROOM_IDLE_TTL_SECONDS = _as_int(
        os.environ.get("COLLAB_ROOM_IDLE_TTL_SECONDS"), 900
    )
    # How often (seconds) the sweeper scans rooms for idle eviction.
    COLLAB_ROOM_SWEEP_SECONDS = _as_int(
        os.environ.get("COLLAB_ROOM_SWEEP_SECONDS"), 60
    )
    # --- Single-writer boundary (design doc §1.6 / §12.3) ---
    # The collab server heartbeats live-room presence into lm_collab_presence;
    # the web process reads it to know a project is in "collaborative mode".
    # How often (seconds) the collab server refreshes the presence heartbeat.
    COLLAB_PRESENCE_HEARTBEAT_SECONDS = _as_int(
        os.environ.get("COLLAB_PRESENCE_HEARTBEAT_SECONDS"), 10
    )
    # A presence row counts as "active" only while it is fresher than this (and
    # has connections > 0). Keep it a small multiple of the heartbeat so a
    # crashed collab server lets projects fall back to classic REST quickly.
    COLLAB_PRESENCE_TTL_SECONDS = _as_int(
        os.environ.get("COLLAB_PRESENCE_TTL_SECONDS"), 30
    )
    # When True, direct REST row mutations (create/patch/delete/move/bulk) on a
    # project that is currently collaborative are rejected with 409 COLLAB_ACTIVE
    # so the CRDT materializer stays the single authoritative writer. Default
    # False: the guard is opt-in and fully backwards compatible; collaborative
    # clients already route their edits through the Y.Doc, not REST.
    COLLAB_REST_GUARD = _as_bool(os.environ.get("COLLAB_REST_GUARD"), False)

    # --- SSE ---
    # How often the SSE endpoint polls task_events for new rows (seconds).
    SSE_POLL_SECONDS = float(os.environ.get("SSE_POLL_SECONDS", "0.5"))
    # Idle heartbeat interval to keep proxies from closing the stream.
    SSE_HEARTBEAT_SECONDS = float(os.environ.get("SSE_HEARTBEAT_SECONDS", "15"))

    @classmethod
    def ensure_dirs(cls) -> None:
        for path in (
            cls.INSTANCE_DIR,
            cls.UPLOAD_DIR,
            cls.REPORT_DIR,
            cls.WORKSPACE_DIR,
            cls.MODEL_DIR,
            cls.POOL_DIR,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)
