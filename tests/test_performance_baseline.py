"""Guards for the local performance baseline. No database connection."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.performance_baseline import (
    BaselineError,
    asset_report,
    percentile,
    validate_database_url,
)


def test_percentile_p50_of_five_samples():
    assert percentile([5, 1, 4, 2, 3], 0.5) == 3


def test_validate_database_url_accepts_explicit_postgres():
    url = "postgresql+psycopg2://silver_app:secret@127.0.0.1:5432/silver_test_platform"
    assert validate_database_url(url) == url


@pytest.mark.parametrize("target", [
    "",
    "sqlite:///local.db",
    "postgresql+psycopg2://silver_app:secret@/silver_test_platform",
    "postgresql+psycopg2://silver_app:secret@127.0.0.1:5432/postgres",
    "postgresql+psycopg2://silver_app:secret@127.0.0.1:5432/app?host=elsewhere",
])
def test_validate_database_url_rejects_unsafe_targets(target):
    with pytest.raises(BaselineError):
        validate_database_url(target)


def test_asset_report_counts_bytes_without_parsing(tmp_path: Path):
    js_path = tmp_path / "bundle.js"
    css_path = tmp_path / "bundle.css"
    js_path.write_bytes(b"function ok(){return 1}")
    css_path.write_bytes(b"body{color:black}")
    report = asset_report(js_path, css_path, parse_javascript=False)
    assert report["univer_js"]["bytes"] == len(b"function ok(){return 1}")
    assert report["univer_js"]["gzip_bytes"] > 0
    assert "node_parse_ms" not in report["univer_js"]
    assert report["univer_css"]["bytes"] == len(b"body{color:black}")
