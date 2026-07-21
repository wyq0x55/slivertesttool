"""Runtime settings for the LAN Test Matrix service layer.

The LAN Test Matrix project used to be a self-contained application with its own
scattered magic numbers. Now that it is expanded into the platform service layer
(:mod:`app.services.lanmatrix`), every one of those knobs is centralised in
:class:`app.config.Config` and driven by the shared ``.env`` file.

This module is a *thin* indirection over that config. It exists so the pure,
Flask-independent service modules (``security``, ``service``, ``batch``,
``excel_service`` …) can stay decoupled from the Flask application object while
still reading their configuration from the one ``.env`` source of truth shared
by both the web process and the Huey worker.

Values are resolved once at import time; changing ``.env`` requires a process
restart, which matches how the rest of the platform treats its configuration.
"""

from __future__ import annotations

try:
    # Normal case: imported as ``app.services.lanmatrix.settings`` inside the
    # full Flask application package.
    from ...config import Config
except ImportError:
    # Pure-module test harness (``tests/lm_helpers.py``) loads this module under
    # a synthetic top-level package, so the package-relative ``...config`` walks
    # beyond it. ``app.config`` is dependency-free (stdlib only), so load it
    # directly by file path without triggering ``app/__init__`` (which would
    # pull in Flask / SQLAlchemy and defeat the point of the pure harness).
    import importlib.util as _ilu
    import pathlib as _pathlib

    _config_path = _pathlib.Path(__file__).resolve().parents[2] / "config.py"
    _spec = _ilu.spec_from_file_location("_lm_pure_config", _config_path)
    _config_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_config_mod)
    Config = _config_mod.Config

# --- Account lockout ------------------------------------------------------- #
LOCK_THRESHOLD: int = Config.LM_LOCK_THRESHOLD
LOCK_MINUTES: int = Config.LM_LOCK_MINUTES

# --- Pagination ------------------------------------------------------------ #
PAGE_SIZE: int = Config.LM_PAGE_SIZE
PAGE_SIZE_MAX: int = Config.LM_PAGE_SIZE_MAX

# --- Batch / import -------------------------------------------------------- #
BATCH_SAMPLE_LIMIT: int = Config.LM_BATCH_SAMPLE_LIMIT
IMPORT_ERROR_LIMIT: int = Config.LM_IMPORT_ERROR_LIMIT

# --- User-supplied regex guards -------------------------------------------- #
REGEX_MAX_LEN: int = Config.LM_REGEX_MAX_LEN
REGEX_TIMEOUT: float = Config.LM_REGEX_TIMEOUT

# --- Filenames ------------------------------------------------------------- #
FILENAME_MAX_LEN: int = Config.LM_FILENAME_MAX_LEN

# --- Test-Matrix (Excel) bridge defaults ----------------------------------- #
TM_ID_PREFIX: str = Config.LM_TM_ID_PREFIX
TM_SUMMARY_SHEET: str = Config.LM_TM_SUMMARY_SHEET

# --- Self-service registration (LAN users) --------------------------------- #
ALLOW_REGISTRATION: bool = Config.LM_ALLOW_REGISTRATION
REGISTRATION_DEFAULT_STATUS: str = Config.LM_REGISTRATION_DEFAULT_STATUS
PASSWORD_MIN_LEN: int = Config.LM_PASSWORD_MIN_LEN
USERNAME_PATTERN: str = Config.LM_USERNAME_PATTERN
