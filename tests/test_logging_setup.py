"""Cover the single logging configuration shared by every process.

The bug being prevented: three unrelated ``basicConfig`` calls, no file handler
anywhere, and whichever entry point ran first silently disabled the others (the
collab process logged without timestamps for exactly this reason). A restart
then destroyed the only copy of the logs.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def fresh_root():
    """Give each test a pristine root logger and restore it afterwards."""
    from app import logging_setup

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_role = getattr(root, logging_setup._CONFIGURED_FLAG, None)

    for handler in list(root.handlers):
        root.removeHandler(handler)
    if hasattr(root, logging_setup._CONFIGURED_FLAG):
        delattr(root, logging_setup._CONFIGURED_FLAG)

    yield logging_setup

    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    if saved_role is not None:
        setattr(root, logging_setup._CONFIGURED_FLAG, saved_role)
    elif hasattr(root, logging_setup._CONFIGURED_FLAG):
        delattr(root, logging_setup._CONFIGURED_FLAG)


class _Cfg:
    def __init__(self, tmp_path, **kw):
        self.LOG_DIR = tmp_path
        self.LOG_LEVEL = kw.get("LOG_LEVEL", "INFO")
        self.LOG_CONSOLE_LEVEL = kw.get("LOG_CONSOLE_LEVEL", "")
        self.LOG_THIRD_PARTY_LEVEL = kw.get("LOG_THIRD_PARTY_LEVEL", "WARNING")
        self.LOG_MAX_BYTES = kw.get("LOG_MAX_BYTES", 1024)
        self.LOG_BACKUP_COUNT = kw.get("LOG_BACKUP_COUNT", 2)
        self.LOG_TO_FILE = kw.get("LOG_TO_FILE", True)


def test_writes_a_rotating_file_per_role(fresh_root, tmp_path):
    setup = fresh_root
    path = setup.configure("worker", _Cfg(tmp_path))

    assert path == tmp_path / "worker.log"
    logging.getLogger("silvetestapp.worker").info("hello from the worker")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello from the worker" in path.read_text(encoding="utf-8")


def test_records_carry_a_timestamp_and_logger_name(fresh_root, tmp_path):
    """The collab process used to emit bare ``INFO:collab.x:msg`` lines."""
    setup = fresh_root
    path = setup.configure("collab", _Cfg(tmp_path))

    logging.getLogger("collab.materializer").info("materialized project 2")
    for handler in logging.getLogger().handlers:
        handler.flush()
    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]

    assert "collab.materializer" in line
    assert "INFO" in line
    assert line[:4].isdigit()          # starts with a %Y timestamp
    assert not line.startswith("INFO:")


def test_second_call_is_a_no_op(fresh_root, tmp_path):
    """create_app must not be able to steal a role an entry point claimed."""
    setup = fresh_root
    first = setup.configure("web", _Cfg(tmp_path))
    handlers = list(logging.getLogger().handlers)

    second = setup.configure("cli", _Cfg(tmp_path))

    assert second == first
    assert setup.current_role() == "web"
    assert logging.getLogger().handlers == handlers, "handlers were duplicated"


def test_force_reconfigures_without_duplicating_handlers(fresh_root, tmp_path):
    setup = fresh_root
    setup.configure("web", _Cfg(tmp_path))
    before = len(logging.getLogger().handlers)

    setup.configure("cli", _Cfg(tmp_path), force=True)

    assert setup.current_role() == "cli"
    assert len(logging.getLogger().handlers) == before


def test_file_logging_can_be_disabled(fresh_root, tmp_path):
    setup = fresh_root
    assert setup.configure("cli", _Cfg(tmp_path, LOG_TO_FILE=False)) is None
    assert not list(tmp_path.glob("*.log"))


def test_third_party_loggers_are_quietened(fresh_root, tmp_path):
    setup = fresh_root
    setup.configure("web", _Cfg(tmp_path, LOG_LEVEL="DEBUG"))

    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
    assert logging.getLogger("werkzeug").level == logging.WARNING
    # The application's own loggers keep the requested level.
    assert logging.getLogger().level == logging.DEBUG


def test_an_unwritable_log_dir_degrades_to_console(fresh_root, tmp_path,
                                                   monkeypatch):
    """A bad LOG_DIR must never stop a process from starting."""
    setup = fresh_root

    def boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.mkdir", boom)
    assert setup.configure("web", _Cfg(tmp_path)) is None
    assert logging.getLogger().handlers, "console handler must survive"


def test_collab_log_level_env_is_still_honoured(fresh_root, tmp_path,
                                                monkeypatch):
    setup = fresh_root
    monkeypatch.setenv("COLLAB_LOG_LEVEL", "DEBUG")
    setup.configure("collab", _Cfg(tmp_path, LOG_LEVEL="INFO"))

    assert logging.getLogger().level == logging.DEBUG


def test_unknown_level_falls_back_instead_of_raising(fresh_root, tmp_path):
    setup = fresh_root
    setup.configure("web", _Cfg(tmp_path, LOG_LEVEL="LOUD"))
    assert logging.getLogger().level == logging.INFO
