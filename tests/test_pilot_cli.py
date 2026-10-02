from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def cli():
    return import_module("scripts.efficiency_pilot")


def input_payload():
    return {
        "schema_version": 1, "project_id": 7, "modules": ["module-a", "module-b"],
        "model": {"model_id": 3, "version": "v1", "sha256": "a" * 64},
        "viewpoints": [{"item_id": item_id, "version": 2,
                        "module": "module-a" if item_id <= 10 else "module-b",
                        "document_revision": "doc-v1", "approval_reference": "operator-review-1"}
                       for item_id in range(1, 21)],
    }


def write_input(tmp_path, payload=None):
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(payload if payload is not None else input_payload()), encoding="utf-8")
    return path


def test_template_contains_no_invented_real_ids_or_measurements(capsys):
    assert cli().main(["--template"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_id"] is None
    assert len(payload["viewpoints"]) == 20
    assert all(item["item_id"] is None and item["approval_reference"] is None for item in payload["viewpoints"])
    assert all(value is None for value in payload["timings"].values())


def test_schema_is_the_canonical_typed_schema(capsys):
    from app.services.pilot_contract import PilotInput
    assert cli().main(["--schema"]) == 0
    assert json.loads(capsys.readouterr().out) == PilotInput.model_json_schema()


def test_offline_input_emits_incomplete_report_without_database(tmp_path, monkeypatch, capsys):
    path = write_input(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://private:do-not-leak@127.0.0.1:1/production")
    monkeypatch.setenv("PILOT_DATABASE_URL", "postgresql://private:do-not-leak@127.0.0.1:1/production")
    assert cli().main([str(path)]) == 3
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["source"] == "unverified"
    assert result["measurement"]["complete"] is False
    assert "do-not-leak" not in captured.out + captured.err


@pytest.mark.parametrize("contents", ["null", "{", '{"project_id": 7, "project_id": 8}',
                                     '{"schema_version":1,"timings":{"manual_baseline_minutes":NaN}}',
                                     '{"schema_version":1,"timings":{"manual_baseline_minutes":Infinity}}'])
def test_invalid_or_ambiguous_json_is_a_safe_input_error(tmp_path, capsys, contents):
    path = tmp_path / "pilot.json"
    path.write_text(contents, encoding="utf-8")
    assert cli().main([str(path)]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "Invalid pilot input" in captured.err
    assert "Traceback" not in captured.err


def test_unreadable_input_is_a_safe_error(tmp_path, capsys):
    assert cli().main([str(tmp_path / "absent.json")]) == 2
    assert "Cannot read pilot input" in capsys.readouterr().err


def test_input_byte_limit_is_enforced(tmp_path, capsys):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    assert cli().main([str(path)]) == 2
    assert "Invalid pilot input" in capsys.readouterr().err


def test_schema_errors_never_echo_raw_injected_payload(tmp_path, capsys):
    payload = input_payload()
    payload["trusted_snapshot"] = {"credential": "do-not-leak"}
    path = write_input(tmp_path, payload)
    assert cli().main([str(path)]) == 2
    captured = capsys.readouterr()
    assert "do-not-leak" not in captured.err
    assert "Invalid pilot input" in captured.err


@pytest.mark.parametrize("flags", [[], ["--template", "--schema"], ["--live"], ["--actor-id", "1"]])
def test_invalid_cli_combinations_fail_before_access(flags):
    with pytest.raises(SystemExit) as failure:
        cli().main(flags)
    assert failure.value.code == 2


def test_live_requires_explicit_pilot_database_not_default_database(tmp_path, monkeypatch, capsys):
    path = write_input(tmp_path)
    monkeypatch.delenv("PILOT_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://private:do-not-leak@127.0.0.1:5432/production")
    assert cli().main([str(path), "--live", "--actor-id", "1"]) == 2
    captured = capsys.readouterr()
    assert "PILOT_DATABASE_URL" in captured.err
    assert "do-not-leak" not in captured.err


@pytest.mark.parametrize("dsn", ["sqlite:///production.db", "postgresql://private:do-not-leak@127.0.0.1:1/production"])
def test_live_configuration_or_connection_failures_redact_credentials(tmp_path, monkeypatch, capsys, dsn):
    path = write_input(tmp_path)
    monkeypatch.setenv("PILOT_DATABASE_URL", dsn)
    assert cli().main([str(path), "--live", "--actor-id", "1"]) == 2
    captured = capsys.readouterr()
    assert "do-not-leak" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_offline_subprocess_never_connects_poisoned_database_or_provider(tmp_path):
    path = write_input(tmp_path)
    environment = {**os.environ, "DATABASE_URL": "postgresql://private:do-not-leak@127.0.0.1:1/production",
                   "HUEY_DATABASE_URL": "postgresql://private:do-not-leak@127.0.0.1:1/production",
                   "PILOT_DATABASE_URL": "postgresql://private:do-not-leak@127.0.0.1:1/production",
                   "OPENAI_API_KEY": "do-not-leak", "RUNNER_BACKEND": "silver"}
    result = subprocess.run([sys.executable, "-m", "scripts.efficiency_pilot", str(path)],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 3
    assert json.loads(result.stdout)["measurement"]["rollout_approved"] is False
    assert "do-not-leak" not in result.stdout + result.stderr
