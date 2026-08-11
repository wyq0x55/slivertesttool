"""One logging configuration for every process of the platform.

Why this module exists
----------------------
Logging used to be configured in three unrelated places, and none of them wrote
to disk:

* ``app.create_app`` called ``logging.basicConfig`` with a hard-coded ``INFO``
  and a timestamped format;
* ``run_collab.py`` called ``basicConfig`` again, *before* ``create_app`` -- so
  the one in ``create_app`` became a no-op and the collab process logged without
  timestamps (``INFO:collab.materializer:...``);
* ``silver_json_runner.py`` configures its own file logger inside the Silver
  process, which is unrelated to any of this.

The practical consequence was that a process restart destroyed the only copy of
the logs: there was not a single ``FileHandler`` in the project. Diagnosing an
intermittent problem (a write-back race, a flaky pooled Silver instance) then
depends on a terminal window still being open.

What it does
------------
:func:`configure` installs, exactly once per process:

* a console handler (stderr), and
* a size-based rotating file handler at ``LOG_DIR/<role>.log``.

``role`` names the process ("web", "worker", "collab", "cli"), so the three
long-running processes never share one file handle -- on Windows a rotating
handler cannot rename a file another process still holds open.

Levels are environment-driven: ``LOG_LEVEL`` (application loggers),
``LOG_CONSOLE_LEVEL`` (console only, defaults to ``LOG_LEVEL``) and
``LOG_THIRD_PARTY_LEVEL`` (sqlalchemy / werkzeug / huey / uvicorn, which are
chatty at INFO). ``COLLAB_LOG_LEVEL`` is still honoured for the collab role so
existing deployments keep working.

Calling it more than once is safe and is a no-op: the first caller in a process
wins, which is what makes the ``run_*.py`` entry points and ``create_app`` able
to call it defensively without fighting each other.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

#: Process roles that get their own log file.
ROLES = ("web", "worker", "collab", "cli")

_LOG_FORMAT = ("%(asctime)s %(levelname)-8s %(name)s [%(process)d] "
               "%(message)s")
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Third-party loggers that are unusable at INFO in this application.
_THIRD_PARTY = ("sqlalchemy", "sqlalchemy.engine", "werkzeug", "huey",
                "uvicorn", "uvicorn.access", "uvicorn.error", "asyncio",
                "urllib3", "PIL")

#: Set on the root logger once configured, so a second call is a no-op.
_CONFIGURED_FLAG = "_silvetestapp_logging_role"


def current_role() -> Optional[str]:
    """Return the role this process was configured with (``None`` if not yet)."""
    return getattr(logging.getLogger(), _CONFIGURED_FLAG, None)


def _level(name: str, default: int = logging.INFO) -> int:
    """Resolve a level name to its numeric value, tolerating junk."""
    if not name:
        return default
    value = logging.getLevelName(str(name).strip().upper())
    return value if isinstance(value, int) else default


def configure(role: str = "cli", config=None, *, force: bool = False) -> Path | None:
    """Configure logging for this process. Returns the log file path (or None).

    ``role`` selects the log file name and must be one of :data:`ROLES`; an
    unknown role is accepted (and used verbatim) so a one-off script can keep
    its own file. ``config`` is the application config object/class; when it is
    ``None`` the values are read from the environment directly, which lets a
    process configure logging *before* importing the app.

    Idempotent: the first call in a process wins unless ``force`` is set.
    """
    root = logging.getLogger()
    if getattr(root, _CONFIGURED_FLAG, None) is not None and not force:
        return getattr(root, "_silvetestapp_logging_path", None)

    role = (role or "cli").strip() or "cli"

    def _cfg(key: str, default):
        if config is not None:
            value = getattr(config, key, None)
            if value is not None:
                return value
        return os.environ.get(key, default)

    app_level = _level(str(_cfg("LOG_LEVEL", "INFO")))
    # The collab process historically had its own knob; keep honouring it so
    # existing run scripts and service definitions do not silently change level.
    if role == "collab" and os.environ.get("COLLAB_LOG_LEVEL"):
        app_level = _level(os.environ["COLLAB_LOG_LEVEL"], app_level)
    console_level = _level(str(_cfg("LOG_CONSOLE_LEVEL", "")), app_level)
    third_party_level = _level(str(_cfg("LOG_THIRD_PARTY_LEVEL", "WARNING")),
                               logging.WARNING)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Replace whatever an earlier basicConfig (ours or a library's) installed;
    # leaving them attached would duplicate every line.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best effort
            pass

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    path: Path | None = None
    to_file = _cfg("LOG_TO_FILE", "1")
    if str(to_file).strip().lower() not in ("0", "false", "no", "off"):
        try:
            log_dir = Path(_cfg("LOG_DIR", "instance/logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"{role}.log"
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=int(_cfg("LOG_MAX_BYTES", 10 * 1024 * 1024)),
                backupCount=int(_cfg("LOG_BACKUP_COUNT", 10)),
                encoding="utf-8",
                delay=True,
            )
            file_handler.setLevel(app_level)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            # A read-only or missing log directory must never stop the platform
            # from starting; degrade to console-only and say so.
            path = None
            logging.getLogger(__name__).warning(
                "file logging disabled: could not open the log directory (%s)",
                exc)

    root.setLevel(min(app_level, console_level))
    for name in _THIRD_PARTY:
        logging.getLogger(name).setLevel(third_party_level)

    setattr(root, _CONFIGURED_FLAG, role)
    setattr(root, "_silvetestapp_logging_path", path)

    logging.getLogger("silvetestapp.logging").info(
        "logging configured: role=%s level=%s console=%s file=%s",
        role, logging.getLevelName(app_level),
        logging.getLevelName(console_level), path or "(disabled)")
    return path
