"""Subprocess CLI against a real loopback Flask server and disposable database."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest
from werkzeug.serving import make_server


@pytest.fixture
def cli_server(app_ctx):
    from app.extensions import db
    from app.models import LMUser

    with app_ctx.app_context():
        for username in ("cli-owner", "cli-reader", "cli-outsider"):
            user = LMUser(username=username, display_name=username, password_hash="unused")
            user.set_password("cli-test-password")
            db.session.add(user)
        db.session.commit()
    server = make_server("127.0.0.1", 0, app_ctx, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def invoke(*arguments, username="cli-owner", expected=0):
        env = dict(os.environ, SILVER_CLI_URL=f"http://127.0.0.1:{server.server_port}",
                   SILVER_CLI_USERNAME=username, SILVER_CLI_PASSWORD="cli-test-password")
        completed = subprocess.run([sys.executable, "-m", "silver_cli", *arguments],
                                   capture_output=True, text=True, env=env, timeout=20)
        assert completed.returncode == expected, completed.stdout + completed.stderr
        assert "cli-test-password" not in completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    yield invoke, app_ctx
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _project(invoke):
    payload = invoke("call", "projects.create_project", "--json",
                     '{"code":"CLI","name":"CLI integration"}', "--confirm")
    return payload["data"]["project"]["id"]


def test_cli_project_edit_conflict_auth_and_permission_parity(cli_server):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember
    from app.services.lanmatrix import fields, fields_service

    invoke, application = cli_server
    project_id = _project(invoke)
    project_param = f"project_id={project_id}"
    with application.app_context():
        project = db.session.get(Project, project_id)
        owner = LMUser.query.filter_by(username="cli-owner").one()
        reader = LMUser.query.filter_by(username="cli-reader").one()
        db.session.add(ProjectMember(project_id=project_id, user_id=reader.id, role="reader"))
        fields_service.ensure_fields(owner, project, fields.TEST_FIELDS)
        db.session.commit()
    invoke("call", "fields.add_field", "--param", project_param, "--json",
           '{"field_key":"cli_note","display_name":"CLI note","data_type":"text","sheet":"test"}', "--confirm")
    created = invoke("call", "items.create_item", "--param", project_param,
                     "--json", '{"draft":true,"values":{"case_id":"CLI-1","cli_note":"initial"}}', "--confirm")["data"]["item"]
    item_param = f"item_id={created['id']}"
    update = json.dumps({"version": created["version"], "changes": {"cli_note": "updated"}})
    changed = invoke("call", "items.patch_item", "--param", project_param, "--param", item_param,
                     "--json", update, "--confirm")
    assert changed["data"]["item"]["cli_note"] == "updated"
    conflict = invoke("call", "items.patch_item", "--param", project_param, "--param", item_param,
                      "--json", update, "--confirm", expected=5)
    assert conflict["status"] == 409
    denied = invoke("call", "items.patch_item", "--param", project_param, "--param", item_param,
                    "--json", update, "--confirm", username="cli-reader", expected=4)
    assert denied["status"] == 403
    invoke("call", "projects.get_project", "--param", project_param, username="cli-reader")
    invoke("call", "projects.get_project", "--param", project_param, username="cli-outsider", expected=4)
    invoke("call", "admin_console.admin_list_users", expected=4)


def test_cli_excel_download_upload_preview_commit(cli_server, tmp_path):
    from app.extensions import db
    from app.models import LMUser, Project
    from app.services.lanmatrix import fields, fields_service, service

    invoke, application = cli_server
    project_id = _project(invoke)
    with application.app_context():
        owner = LMUser.query.filter_by(username="cli-owner").one()
        project = db.session.get(Project, project_id)
        fields_service.ensure_fields(owner, project, fields.TEST_FIELDS)
        service.create_item(owner, project, {"case_id": "CLI-EXCEL", "title": "round trip"}, draft=True)
    output = tmp_path / "export.xlsx"
    invoke("call", "imports_exports.create_export", "--param", f"project_id={project_id}",
           "--output", str(output), "--confirm")
    assert output.read_bytes().startswith(b"PK")
    preview = invoke("call", "imports_exports.create_import", "--param", f"project_id={project_id}",
                     "--file", f"file={output}", "--form", "mode=upsert", "--confirm")
    job_id = preview["data"]["job"]["id"]
    invoke("call", "imports_exports.get_import", "--param", f"job_id={job_id}")
    committed = invoke("call", "imports_exports.commit_import", "--param", f"job_id={job_id}", "--confirm")
    assert committed["success"] is True


def test_cli_ai_draft_wait_and_reject_preserve_audit(cli_server):
    from app.extensions import db
    from app.models import LMUser
    from app.models.ai_draft import AiDraft

    invoke, application = cli_server
    project_id = _project(invoke)
    with application.app_context():
        owner = LMUser.query.filter_by(username="cli-owner").one()
        draft = AiDraft(project_id=project_id, scenario="viewpoint", status="pending",
                        created_by=owner.id, input_json="{}", output_json="{}")
        db.session.add(draft)
        db.session.commit()
        draft_id = draft.id
    invoke("wait", "ai.get_draft", "--param", f"draft_id={draft_id}",
           "--status-path", "data.status", "--success", "pending", "--failure", "error", "--pending", "running")
    rejected = invoke("call", "ai.reject_draft", "--param", f"draft_id={draft_id}",
                      "--json", '{"note":"CLI integration rejection"}', "--confirm")
    assert rejected["data"]["status"] == "rejected"
    with application.app_context():
        stored = db.session.get(AiDraft, draft_id)
        assert stored.review_note == "CLI integration rejection"
        assert stored.reviewed_by is not None


def test_cli_saved_model_submit_wait_and_retest(cli_server, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models import Task, TestItemRow
    from app.services import run_evidence_service

    invoke, application = cli_server
    project_id = _project(invoke)
    monkeypatch.setattr(application.config_obj, "MODEL_DIR", tmp_path / "saved-models")
    source = tmp_path / "source.sil"
    source.write_text("cli model fixture", encoding="utf-8")
    registered = invoke("call", "models.add_project_model", "--param", f"project_id={project_id}",
                        "--json", json.dumps({"name": "cli-plant", "version": "v1", "path": str(source)}), "--confirm")
    assert registered["success"] is True
    with application.app_context():
        db.session.add(TestItemRow(project_id=project_id, case_id="CLI-RUN", sheet="test",
                                  custom_values={"steps": {"steps": [{"no": 1}]}}))
        db.session.commit()
    queued = []
    monkeypatch.setattr("app.routes.lanmatrix.tasks._enqueue_task", lambda task: queued.append((task.id, task.run_count)))
    source.write_text("edited original source", encoding="utf-8")
    submitted = invoke("call", "tasks.run_selected_tasks", "--param", f"project_id={project_id}",
                       "--json", '{"test_ids":["CLI-RUN"],"model":"cli-plant@v1"}', "--confirm")
    assert submitted["data"]["errors"] == []
    task_key = submitted["data"]["created"][0]["task_id"]
    assert len(queued) == 1
    with application.app_context():
        task = db.session.get(Task, queued[0][0])
        evidence = run_evidence_service.read_evidence(task)
        from pathlib import Path
        assert Path(evidence["model"]["execution_path"]).read_text(encoding="utf-8") == "cli model fixture"
        task.status = "failed"
        db.session.commit()
    invoke("wait", "tasks.project_task_status", "--param", f"project_id={project_id}",
           "--param", f"task_key={task_key}", "--status-path", "data.task.status",
           "--success", "passed", "--failure", "failed", "--pending", "queued", "--pending", "running", expected=8)
    retested = invoke("call", "tasks.rerun_selected_tasks", "--param", f"project_id={project_id}",
                     "--json", json.dumps({"task_keys": [task_key]}), "--confirm")
    assert retested["data"]["errors"] == []
    assert queued[-1][1] == 2
    invoke("call", "tasks.cancel_project_task", "--param", f"project_id={project_id}",
           "--param", f"task_key={task_key}", "--confirm")
