from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from scripts import check_sensitive_content as gate


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_sensitive_content.py"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_current_repository_passes_sensitive_content_gate():
    proc = _run_cli()
    assert proc.returncode == 0, (
        f"Repository sensitive-content gate failed:\n{proc.stderr}"
    )


def test_clean_synthetic_tree_passes(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "example.py").write_text(
        'API_KEY_ENV = "OPENAI_API_KEY"\n'
        'PLACEHOLDER = "sk-placeholder"\n'
        'TASK_ID = "task-0123456789abcdef0123456789abcdef"\n',
        encoding="utf-8",
    )
    # Vendored UMD, lockfile, and explicit fixture paths should be skipped for content scan.
    (tmp_path / "uv.lock").write_text("sk-" + ("A" * 40) + "\n", encoding="utf-8")
    vendor_dir = tmp_path / "app" / "static" / "vendor"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "bundle.umd.js").write_text("ghp_" + ("b" * 36) + "\n", encoding="utf-8")
    fixture_dir = tmp_path / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "sample.txt").write_text("AKIA" + ("0" * 16) + "\n", encoding="utf-8")

    proc = _run_cli("--root", str(tmp_path), "--paths", str(tmp_path))
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "rel_report_path",
    [
        "reports/summary.md",
        "reports/nested/audit.docx",
        "reports/export.doc",
        "reports/evidence.pdf",
        "reports/view.html",
    ],
)
def test_detects_prohibited_reports_artifacts(tmp_path: Path, rel_report_path: str):
    target = tmp_path / rel_report_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"benign report payload")

    proc = _run_cli("--root", str(tmp_path), "--paths", str(target))
    assert proc.returncode != 0
    assert "tracked_report_artifact" in proc.stderr


@pytest.mark.parametrize(
    ("label", "secret_line", "expected_rule"),
    [
        (
            "rsa_key",
            "-----BEGIN " + "RSA PRIVATE KEY-----",
            "private_key_header",
        ),
        (
            "openssh_key",
            "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "private_key_header",
        ),
        (
            "pkcs8_key",
            "-----BEGIN " + "PRIVATE KEY-----",
            "private_key_header",
        ),
        (
            "openai_key",
            "OPENAI_API_KEY=" + "sk-" + ("a1B2" * 9),
            "openai_secret_key",
        ),
        (
            "ghp_token",
            'token = "' + "ghp_" + ("Z9x8" * 9) + '"',
            "github_classic_pat",
        ),
        (
            "github_pat",
            "Authorization: Bearer " + "github_pat_" + ("Ab_1" * 11),
            "github_fine_grained_pat",
        ),
        (
            "aws_akia",
            "aws_access_key_id = " + "AKIA" + "0123456789ABCDEF",
            "aws_access_key_id",
        ),
        (
            "procurement_doc",
            "# " + "DGX_Spark_" + "\u5185\u7f51\u90e8\u7f72" + " plan",
            "internal_procurement_marker",
        ),
    ],
)
def test_detects_sensitive_content_patterns_without_echoing_secret(
    tmp_path: Path,
    label: str,
    secret_line: str,
    expected_rule: str,
):
    sample = tmp_path / f"{label}.txt"
    sample.write_text(f"line 1 ok\n{secret_line}\n", encoding="utf-8")

    proc = _run_cli("--paths", str(sample))
    assert proc.returncode != 0
    assert expected_rule in proc.stderr
    assert f"{label}.txt:2" in proc.stderr
    # Ensure raw credential values are never echoed to CI logs.
    if expected_rule not in {"private_key_header", "internal_procurement_marker"}:
        raw_token = secret_line.split()[-1].strip('"').split("=")[-1]
        assert raw_token not in proc.stderr


def test_gate_self_files_pass_direct_scan():
    findings = gate.scan_paths([SCRIPT_PATH, Path(__file__)])
    assert findings == []


def test_preserves_leading_dot_on_dotfiles(tmp_path: Path):
    dotfile = tmp_path / ".mcp.json"
    dotfile.write_text('{"token": "' + "github_pat_" + ("Ab_1" * 11) + '"}\n', encoding="utf-8")

    proc = _run_cli("--root", str(tmp_path), "--paths", str(dotfile))
    assert proc.returncode != 0
    assert ".mcp.json:1: [github_fine_grained_pat]" in proc.stderr
