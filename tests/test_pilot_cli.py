from importlib import import_module
import hashlib
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


@pytest.mark.parametrize("dsn", [
    "postgresql://", "postgresql+psycopg2://", "postgresql:///pilot",
    "postgresql://localhost", "postgresql://localhost/",
    "postgresql://localhost/pilot?host=", "postgresql://localhost/pilot?dbname=",
    "postgresql://localhost/pilot?service=production",
    "postgresql://localhost/pilot?hostaddr=",
])
def test_live_rejects_implicit_or_query_overridden_targets_before_factory(tmp_path, monkeypatch, capsys, dsn):
    import app
    from unittest.mock import Mock
    factory = Mock(side_effect=AssertionError("Application factory must not run"))
    monkeypatch.setattr(app, "create_app", factory)
    monkeypatch.setenv("PILOT_DATABASE_URL", dsn)
    path = write_input(tmp_path)
    assert cli().main([str(path), "--live", "--actor-id", "1"]) == 2
    factory.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out and "Live pilot audit failed" in captured.err


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


@pytest.fixture
def live_selection(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, ProjectModel, TestItemRow
    model_root = tmp_path / "models"
    model_root.mkdir()
    model_path = model_root / "pilot.sil"
    model_path.write_bytes(b"<silver-model/>\n")
    monkeypatch.setenv("MODEL_DIR", str(model_root))
    monkeypatch.setitem(app_ctx.config, "MODEL_DIR", model_root)
    with app_ctx.app_context():
        actor = LMUser(username="pilot-reader", password_hash="fixture-only-not-used", status="active")
        project = Project(code="PILOT-CLI", name="Synthetic CLI pilot", status="active")
        db.session.add_all([actor, project])
        db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=actor.id, role="reader"))
        model = ProjectModel(project_id=project.id, name="pilot-copy", version="v1", sil_path=str(model_path))
        rows = [TestItemRow(project_id=project.id, sheet="test", case_id=f"PC-{position}",
                            module="module-a" if position <= 10 else "module-b", version=2)
                for position in range(1, 21)]
        db.session.add_all([model, *rows])
        db.session.flush()
        payload = {"schema_version": 1, "project_id": project.id, "modules": ["module-a", "module-b"],
                   "model": {"model_id": model.id, "version": "v1",
                             "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest()},
                   "viewpoints": [{"item_id": row.id, "version": row.version, "module": row.module,
                                   "document_revision": "fixture-document-v1",
                                   "approval_reference": "fixture-operator-declaration"} for row in rows]}
        actor_id = actor.id
        db.session.commit()
        db.session.remove()
    path = write_input(tmp_path, payload)
    environment = {**os.environ, "PILOT_DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
    yield path, actor_id, environment, payload, app_ctx


def test_live_subprocess_reads_only_selected_project_and_preserves_versions(live_selection):
    path, actor_id, environment, payload, application = live_selection
    result = subprocess.run([sys.executable, "-m", "scripts.efficiency_pilot", str(path),
                             "--live", "--actor-id", str(actor_id), "--mode", "preflight"],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["source"] == "postgresql_readonly"
    assert report["readiness"]["ready"] is True
    assert report["measurement"]["complete"] is False
    from app.extensions import db
    from app.models import AiDraft, Task, TestItemRow
    with application.app_context():
        rows = TestItemRow.query.filter_by(project_id=payload["project_id"]).all()
        assert len(rows) == 20 and all(row.version == 2 and row.review_status == "" for row in rows)
        assert Task.query.count() == 0 and AiDraft.query.count() == 0
        db.session.remove()


def test_live_denied_actor_does_not_disclose_input_or_evidence(live_selection):
    path, _actor_id, environment, _payload, _application = live_selection
    result = subprocess.run([sys.executable, "-m", "scripts.efficiency_pilot", str(path),
                             "--live", "--actor-id", "99999"],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 2
    assert not result.stdout
    assert "Live pilot audit failed" in result.stderr
    assert "fixture-document" not in result.stderr and "Traceback" not in result.stderr
