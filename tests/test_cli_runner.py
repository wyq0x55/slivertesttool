from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    from silver_cli import runner

    monkeypatch.delenv("SILVER_CLI_URL", raising=False)
    monkeypatch.delenv("SILVER_CLI_USERNAME", raising=False)
    monkeypatch.delenv("SILVER_CLI_PASSWORD", raising=False)
    return runner


def result(capsys):
    return json.loads(capsys.readouterr().out)


def test_offline_catalogue_without_site_packages():
    command = [sys.executable, "-S", "-m", "silver_cli", "list"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["success"] is True
    assert len(payload["data"]["operations"]) > 100


def test_describe_and_filtered_listing(runner, capsys):
    assert runner.main(["list", "--group", "models"]) == 0
    assert all(row["name"].startswith("models.") for row in result(capsys)["data"]["operations"])
    assert runner.main(["describe", "tasks.run_selected_tasks"]) == 0
    assert result(capsys)["data"]["method"] == "POST"


@pytest.mark.parametrize("arguments", [
    [], ["unknown"], ["describe", "absent"],
    ["call", "projects.get_project", "--param", "project_id=oops", "--dry-run"],
    ["call", "projects.get_project", "--dry-run"],
    ["call", "projects.get_project", "--param", "project_id=1", "--param", "project_id=2", "--dry-run"],
    ["call", "projects.get_project", "--param", "project_id=1", "--param", "extra=1", "--dry-run"],
    ["call", "tasks.project_task_status", "--param", "project_id=1", "--param", "task_key=../bad", "--dry-run"],
    ["call", "projects.get_project", "--query", "invalid", "--dry-run"],
    ["call", "projects.create_project", "--json", "null", "--dry-run"],
    ["call", "projects.create_project", "--json", '{"a":1,"a":2}', "--dry-run"],
    ["call", "projects.create_project", "--json", '{"a":NaN}', "--dry-run"],
    ["call", "projects.create_project", "--json", '{"a":1e999}', "--dry-run"],
    ["call", "projects.create_project", "--json", "{}", "--form", "name=x", "--dry-run"],
    ["call", "auth.health", "--timeout", "nan"],
    ["call", "auth.health", "--timeout", "0"],
])
def test_bad_inputs_are_json_and_never_network(runner, monkeypatch, capsys, arguments):
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("network constructed"))
    assert runner.main(arguments) == 2
    assert result(capsys)["success"] is False


def test_dry_run_validates_but_does_not_login_or_echo_secrets(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("network constructed"))
    monkeypatch.setenv("SILVER_CLI_PASSWORD", "do-not-print")
    assert runner.main(["call", "projects.create_project", "--json", '{"name":"x","password":"secret"}', "--dry-run"]) == 0
    output = result(capsys)
    assert output["data"]["dry_run"] is True
    assert output["data"]["method"] == "POST"
    assert "secret" not in json.dumps(output)


def test_writes_require_explicit_confirmation_before_login(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("network constructed"))
    assert runner.main(["call", "projects.create_project", "--json", "{}"]) == 2
    assert result(capsys)["error"]["code"] == "CONFIRM_REQUIRED"


def test_calls_reuse_identity_and_preserve_repeated_fields(runner, monkeypatch, capsys, tmp_path):
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def login(self, username, password):
            calls.append((username, password))

        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"success": True, "data": {"created": 2}, "request_id": "req-test", "error": None}

    monkeypatch.setattr(runner, "Client", FakeClient)
    monkeypatch.setenv("SILVER_CLI_USERNAME", "tester")
    monkeypatch.setenv("SILVER_CLI_PASSWORD", "hidden")
    payload = tmp_path / "payload.json"
    payload.write_text('{"name":"测试"}', encoding="utf-8-sig")
    assert runner.main(["call", "projects.create_project", "--url", "http://localhost:5000", "--json", "@" + str(payload), "--query", "tag=a", "--query", "tag=b", "--confirm"]) == 0
    assert result(capsys)["request_id"] == "req-test"
    assert calls[1] == ("tester", "hidden")
    assert calls[2][2]["query"] == [("tag", "a"), ("tag", "b")]
    assert calls[2][2]["json_body"] == {"name": "测试"}


def test_stdin_json_and_sensitive_response_redaction(runner, monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            assert kwargs["json_body"] == {"password": "input-secret"}
            return {"success": True, "data": {"csrf_token": "csrf-secret", "password": "input-secret"}}

    monkeypatch.setattr(runner, "Client", FakeClient)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"password":"input-secret"}'))
    assert runner.main(["call", "auth.login", "--url", "http://localhost", "--json", "@-", "--confirm"]) == 0
    rendered = json.dumps(result(capsys))
    assert "input-secret" not in rendered and "csrf-secret" not in rendered


def test_download_requires_destination_before_network(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("network constructed"))
    assert runner.main(["call", "tasks.download_project_task", "--param", "project_id=1", "--param", "task_key=T000001"]) == 2
    assert result(capsys)["error"]["code"] == "OUTPUT_REQUIRED"


def test_wait_is_bounded_read_only_and_preserves_failed_task(runner, monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            return {"success": True, "data": {"task": {"status": "failed"}}, "error": None}

    monkeypatch.setattr(runner, "Client", FakeClient)
    args = ["wait", "tasks.project_task_status", "--param", "project_id=1", "--param", "task_key=T000001", "--url", "http://localhost", "--status-path", "data.task.status", "--success", "passed", "--failure", "failed", "--pending", "queued", "--pending", "running"]
    assert runner.main(args) == 8
    assert result(capsys)["data"]["task"]["status"] == "failed"
    assert runner.main(["wait", "projects.create_project", "--status-path", "data.status", "--success", "done", "--pending", "running"]) == 2
    result(capsys)


def test_missing_explicit_url_is_not_local_default(runner, capsys):
    assert runner.main(["call", "auth.health"]) == 2
    assert result(capsys)["error"]["code"] == "URL_REQUIRED"


def test_redaction_preserves_token_counts_and_matching_field_hints(runner):
    payload = {"input_tokens": 120, "output_tokens": 33, "max_tokens": 2000,
               "csrf_token": "session-secret", "api_key": "provider-secret"}
    redacted = runner._redact(payload, set())
    assert redacted["input_tokens"] == 120
    assert redacted["output_tokens"] == 33
    assert redacted["max_tokens"] == 2000
    assert redacted["csrf_token"] == "[redacted]"


@pytest.mark.parametrize("events,expected", [
    ([{"event": "end", "data": {}}], 0),
    ([{"event": "end", "data": {"reason": "stream-timeout"}}], 9),
    ([{"event": "progress", "data": {}}], 6),
    ([{"event": "progress", "data": {}}, {"event": "end", "data": {}}], 9),
])
def test_stream_terminal_and_resume_semantics(runner, monkeypatch, capsys, events, expected):
    closed = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            def generate():
                try:
                    yield from events
                finally:
                    closed.append(True)
            return generate()

    monkeypatch.setattr(runner, "Client", FakeClient)
    limit = "1" if len(events) == 2 else "10"
    assert runner.main(["call", "tasks.project_task_stream", "--param", "project_id=1",
                        "--param", "task_key=T000001", "--url", "http://localhost", "--max-events", limit]) == expected
    assert closed == [True]
    for line in capsys.readouterr().out.splitlines():
        assert isinstance(json.loads(line), dict)


@pytest.mark.parametrize("states,expected", [(["running", "passed"], 0), (["unexpected"], 7), ([None], 7), (["running"] * 10, 9)])
def test_wait_transition_error_and_timeout(runner, monkeypatch, capsys, states, expected):
    tick = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: tick.__setitem__(0, tick[0] + seconds))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.states = iter(states)

        def request(self, *args, **kwargs):
            state = next(self.states)
            return {"success": True, "data": {"status": state} if state else {}}

    monkeypatch.setattr(runner, "Client", FakeClient)
    assert runner.main(["wait", "ai.get_draft", "--param", "draft_id=1", "--url", "http://localhost",
                        "--status-path", "data.status", "--success", "passed", "--failure", "failed",
                        "--pending", "running", "--max-wait", "2", "--interval", "1"]) == expected
    assert result(capsys)


def test_every_catalogue_operation_has_executable_dry_run(runner, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("dry-run touched network"))
    for operation in runner.operations():
        arguments = ["call", operation.name, "--dry-run"]
        for name, converter in operation.path_params.items():
            arguments.extend(["--param", name + "=" + ("1" if converter == "int" else "example")])
        if operation.response_kind == "download":
            arguments.extend(["--output", str(tmp_path / "artifact")])
        assert runner.main(arguments) == 0, operation.name
        preview = result(capsys)["data"]
        assert preview["method"] == operation.method
        assert preview["operation"] == operation.name


@pytest.mark.parametrize("extras", [
    ["--success", "pending"], ["--failure", "running"], ["--status-path", "data..status"],
])
def test_wait_rejects_ambiguous_config_before_login(runner, capsys, extras):
    arguments = ["wait", "ai.get_draft", "--param", "draft_id=1", "--status-path", "data.status",
                 "--success", "pending", "--pending", "running", "--dry-run"]
    if extras == ["--success", "pending"]:
        extras = ["--pending", "pending"]
    assert runner.main(arguments + extras) == 2
    assert result(capsys)["success"] is False


def test_invalid_body_and_output_files_are_local_errors(runner, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("network constructed"))
    for arguments in [
        ["call", "auth.health", "--json", "{}"],
        ["call", "auth.health", "--output", str(tmp_path / "out")],
        ["call", "projects.create_project", "--file", f"file={tmp_path / 'missing'}"],
        ["call", "projects.create_project", "--json", "@" + str(tmp_path / "missing")],
        ["call", "auth.health", "--max-events", "0"],
    ]:
        assert runner.main(arguments) == 2
        assert result(capsys)["success"] is False


def test_incomplete_credentials_are_not_sent(runner, monkeypatch, capsys):
    monkeypatch.setenv("SILVER_CLI_USERNAME", "only-user")
    monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: pytest.fail("network constructed"))
    assert runner.main(["call", "auth.health", "--url", "http://localhost"]) == 2
    assert result(capsys)["error"]["code"] == "CREDENTIALS"


def test_response_secrets_are_also_redacted_from_echoed_messages(runner, capsys):
    runner._emit({"data": {"csrf_token": "response-only-secret", "message": "value=response-only-secret"}}, set())
    assert "response-only-secret" not in capsys.readouterr().out
