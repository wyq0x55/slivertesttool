"""Unit tests for the run write-back evidence builder.

These exercise the pure decision logic -- verdict classification, timezone
handling and which columns get stamped -- without a database, so they run in
any environment. The DB-backed paths (row matching, run records, the collab
queue) are covered by the integration suite.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from flask import Flask

from app.services.lanmatrix import run_writeback_service as rws


@pytest.fixture()
def app_ctx():
    app = Flask(__name__)
    app.config["LM_DISPLAY_TZ"] = "Asia/Shanghai"
    app.config["LM_WRITEBACK_LOG_COLUMN"] = True
    with app.app_context():
        yield app


class TestClassify:
    @pytest.mark.parametrize("verdict,expected", [
        ("PASS", "pass"), ("passed", "pass"), ("OK", "pass"),
        ("FAIL", "fail"), ("NG", "fail"),
        ("ERROR", "error"),
        ("Untestable", "untestable"),
        ("cancelled", "cancelled"), ("canceled", "cancelled"),
    ])
    def test_known_verdicts(self, verdict, expected):
        assert rws.classify(verdict) == expected

    def test_unknown_verdict_is_empty_not_an_error(self):
        # An unrecognised verdict must not raise: the run already finished and
        # the dashboard can show it as unclassified.
        assert rws.classify("weird") == ""
        assert rws.classify("") == ""
        assert rws.classify(None) == ""


class TestLocalDate:
    def test_utc_is_shifted_into_the_display_timezone(self, app_ctx):
        # 2026-03-01 23:30 UTC is already 2026-03-02 in Asia/Shanghai (+08:00);
        # reporting the UTC day would file the run under the wrong date.
        assert rws.local_date(datetime(2026, 3, 1, 23, 30)) == "2026-03-02"

    def test_same_day_when_no_rollover(self, app_ctx):
        assert rws.local_date(datetime(2026, 3, 1, 2, 0)) == "2026-03-01"

    def test_none_is_empty(self, app_ctx):
        assert rws.local_date(None) == ""

    def test_unknown_timezone_degrades_to_utc(self, app_ctx):
        app_ctx.config["LM_DISPLAY_TZ"] = "Mars/Olympus"
        assert rws.local_date(datetime(2026, 3, 1, 23, 30)) == "2026-03-01"


class _FakeTask:
    def __init__(self, **kw):
        self.task_key = kw.get("task_key", "T000123")
        self.submitter = kw.get("submitter", "alice")
        self.submitter_id = kw.get("submitter_id")
        self.sil_name = kw.get("sil_name", "")
        self.project_id = kw.get("project_id", 1)
        self.test_id = kw.get("test_id", "TC-1")
        self.finished_at = kw.get("finished_at", datetime(2026, 3, 1, 2, 0))


class TestBuildRowValues:
    def test_stamps_the_full_evidence_set(self, app_ctx, monkeypatch):
        monkeypatch.setattr(rws, "_model_identity", lambda t: ("host", "v1.2.0"))
        values = rws.build_row_values(_FakeTask(), "PASS")
        assert values == {
            "result": "PASS",
            "executor": "alice",
            "exec_date": "2026-03-01",
            "version_label": "v1.2.0",
            "log": "T000123",
        }

    def test_unversioned_model_does_not_blank_a_manual_label(
            self, app_ctx, monkeypatch):
        # Writing "" would erase a version a human had typed in by hand, so an
        # unknown version must simply not be part of the write.
        monkeypatch.setattr(rws, "_model_identity", lambda t: ("host", ""))
        assert "version_label" not in rws.build_row_values(_FakeTask(), "PASS")

    def test_log_column_can_be_switched_off(self, app_ctx, monkeypatch):
        monkeypatch.setattr(rws, "_model_identity", lambda t: ("", ""))
        app_ctx.config["LM_WRITEBACK_LOG_COLUMN"] = False
        assert "log" not in rws.build_row_values(_FakeTask(), "PASS")

    def test_verdict_is_truncated_to_the_column_width(self, app_ctx, monkeypatch):
        monkeypatch.setattr(rws, "_model_identity", lambda t: ("", ""))
        values = rws.build_row_values(_FakeTask(), "X" * 100)
        assert len(values["result"]) == 24
