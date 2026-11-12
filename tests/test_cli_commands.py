from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest


PROJECT_PATH = "/api/v1/projects/7"
ITEM_PATH = PROJECT_PATH + "/items"
TASK_PATH = PROJECT_PATH + "/tasks"
STEPS = {
    "input_signals": [{"name": "speed"}],
    "expected_signals": [{"name": "ready"}],
    "steps": [{"description": "first"}, {"description": "second"}],
}
ITEM = {"id": 42, "case_id": "CASE-42", "version": 9, "steps": STEPS}


def envelope(data, request_id="req-commands"):
    return {"success": True, "data": data, "error": None, "request_id": request_id}


def item_page(items=None, **metadata):
    return {"items": [copy.deepcopy(ITEM)] if items is None else items,
            "total": 1, "page": 1, "page_size": 100, **metadata}


def result(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.fixture
def runner(monkeypatch):
    from silver_cli import runner

    for name in ("SILVER_CLI_URL", "SILVER_CLI_USERNAME", "SILVER_CLI_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return runner


@pytest.fixture
def transport(monkeypatch):
    from silver_cli.transport import Client

    state = {"constructed": [], "logins": [], "calls": [], "responses": []}

    def initialize(self, *args, **kwargs):
        state["constructed"].append((args, kwargs))

    def login(self, username, password):
        state["logins"].append((username, password))

    def request(self, method, path, **kwargs):
        state["calls"].append((method, path, kwargs))
        assert state["responses"], "Unexpected HTTP request: " + method + " " + path
        response = state["responses"].pop(0)
        if isinstance(response, Exception):
            raise response
        return envelope(copy.deepcopy(response))

    monkeypatch.setattr(Client, "__init__", initialize)
    monkeypatch.setattr(Client, "login", login)
    monkeypatch.setattr(Client, "request", request)
    return state


@pytest.fixture
def offline(monkeypatch):
    from silver_cli.transport import Client

    def forbidden(*args, **kwargs):
        pytest.fail("Offline command constructed the HTTP transport")

    monkeypatch.setattr(Client, "__init__", forbidden)


@pytest.mark.parametrize(("arguments", "responses", "expected_calls"), [
    (["project", "list"], [{"projects": [{"id": 7}]}],
     [("GET", "/api/v1/projects")]),
    (["project", "show", "7"], [{"project": {"id": 7}}, {"fields": []},
     {"models": []}, {"progress": {"total": 1}}],
     [("GET", PROJECT_PATH), ("GET", PROJECT_PATH + "/fields"),
      ("GET", PROJECT_PATH + "/models"), ("GET", PROJECT_PATH + "/dashboard")]),
    (["item", "list", "7", "--sheet", "test", "--page", "2", "--page-size", "25"],
     [item_page(page=2, page_size=25)], [("GET", ITEM_PATH)]),
    (["item", "show", "7", "42"], [item_page()], [("GET", ITEM_PATH)]),
    (["steps", "show", "7", "42"], [item_page()], [("GET", ITEM_PATH)]),
    (["task", "list", "7"], [{"tasks": [], "total": 0, "truncated": False}],
     [("GET", TASK_PATH)]),
    (["task", "show", "7", "T000001"], [{"task": {"task_id": "T000001"}}],
     [("GET", TASK_PATH + "/T000001/detail")]),
    (["task", "log", "7", "T000001", "--tail", "2"],
     [{"task": {"task_id": "T000001", "events": []}}],
     [("GET", TASK_PATH + "/T000001/detail")]),
    (["task", "rerun", "7", "T000001", "--confirm"],
     [{"created": ["T000001"], "errors": []}],
     [("POST", TASK_PATH + "/rerun-selected")]),
])
def test_command_arguments_dispatch_to_existing_endpoints(
        runner, transport, capsys, monkeypatch, arguments, responses, expected_calls):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = responses
    assert runner.main(arguments) == 0
    assert result(capsys)["success"] is True
    assert [(method, path) for method, path, kwargs in transport["calls"]] == expected_calls
    assert transport["responses"] == []
    assert len(transport["constructed"]) == 1
    if arguments[:2] == ["item", "list"]:
        assert dict(transport["calls"][0][2]["query"]) == {
            "sheet": "test", "page": "2", "page_size": "25"}
    if arguments[:2] == ["task", "rerun"]:
        assert transport["calls"][0][2]["json_body"] == {"task_keys": ["T000001"]}


def test_project_show_aggregates_data_and_logs_in_once(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    monkeypatch.setenv("SILVER_CLI_USERNAME", "tester")
    monkeypatch.setenv("SILVER_CLI_PASSWORD", "secret-password")
    transport["responses"] = [
        {"project": {"id": 7, "name": "CLI project"}},
        {"fields": [{"key": "case_id"}]},
        {"models": [{"name": "engine.sil"}]},
        {"progress": {"total": 42}},
    ]
    assert runner.main(["project", "show", "7", "--timeout", "12"]) == 0
    payload = result(capsys)
    assert transport["logins"] == [("tester", "secret-password")]
    assert transport["constructed"][0][1]["timeout"] == 12
    assert "secret-password" not in json.dumps(payload)
    assert payload["request_id"] == "req-commands"
    assert payload["data"] == {
        "project": {"id": 7, "name": "CLI project"},
        "fields": [{"key": "case_id"}],
        "models": [{"name": "engine.sil"}],
        "dashboard": {"progress": {"total": 42}},
    }


def test_project_create_reads_json_file(runner, transport, capsys, tmp_path):
    payload = tmp_path / "project.json"
    payload.write_text('{"code":"DEMO","name":"测试"}', encoding="utf-8-sig")
    transport["responses"] = [{"project": {"id": 8}}]
    assert runner.main(["project", "create", "--json", "@" + str(payload),
                        "--url", "http://localhost:5000", "--confirm"]) == 0
    assert result(capsys)["success"] is True
    assert transport["calls"][0][:2] == ("POST", "/api/v1/projects")
    assert transport["calls"][0][2]["json_body"] == {"code": "DEMO", "name": "测试"}


@pytest.mark.parametrize("arguments", [
    ["project", "create", "--json", '{"name":"demo"}'],
    ["steps", "set", "7", "42", "--json", json.dumps(STEPS)],
    ["steps", "edit", "7", "42", "--step", "1", "--set", "description=updated"],
    ["task", "rerun", "7", "T000001"],
])
def test_all_writes_require_confirmation_before_any_network(runner, offline, capsys, arguments):
    assert runner.main(arguments) == 2
    assert result(capsys)["error"]["code"] == "CONFIRM_REQUIRED"


@pytest.mark.parametrize("arguments", [
    ["project", "create", "--json", '{"name":"demo","password":"hidden-value"}'],
    ["steps", "set", "7", "42", "--json", json.dumps(STEPS)],
    ["steps", "edit", "7", "42", "--step", "1", "--set", "description=updated"],
    ["task", "rerun", "7", "T000001"],
])
def test_write_dry_runs_do_not_construct_transport(
        runner, offline, capsys, monkeypatch, arguments):
    monkeypatch.setenv("SILVER_CLI_PASSWORD", "hidden-value")
    assert runner.main(arguments + ["--dry-run"]) == 0
    payload = result(capsys)
    assert payload["success"] is True
    assert payload["data"]["dry_run"] is True
    assert "hidden-value" not in json.dumps(payload)


@pytest.mark.parametrize("arguments", [
    ["project"], ["project", "show"], ["project", "show", "oops"],
    ["project", "create"], ["project", "create", "--json", "[]", "--dry-run"],
    ["project", "create", "--json", "{}", "--dry-run", "--confirm"],
    ["item", "list", "7", "--page", "0"],
    ["item", "list", "7", "--page-size", "-1"],
    ["item", "show", "7", "oops"], ["item", "show", "7"],
    ["steps", "show", "7"], ["steps", "set", "7", "42"],
    ["steps", "set", "7", "42", "--json", "null", "--dry-run"],
    ["steps", "set", "7", "42", "--json", '{"steps":NaN}', "--dry-run"],
    ["steps", "set", "7", "42", "--json", "{}", "--dry-run"],
    ["steps", "set", "7", "42", "--json", json.dumps({**STEPS, "steps": ["invalid"]}), "--dry-run"],
    ["steps", "set", "7", "42", "--json", json.dumps(STEPS), "--dry-run", "--confirm"],
    ["steps", "edit", "7", "42", "--step", "1"],
    ["steps", "edit", "7", "42", "--step", "0", "--set", "description=updated", "--dry-run"],
    ["steps", "edit", "7", "42", "--step", "1", "--set", "invalid", "--dry-run"],
    ["steps", "edit", "7", "42", "--step", "1", "--set", "purpose=one", "purpose=two", "--dry-run"],
    ["steps", "edit", "7", "42", "--step", "1", "--set", "purpose=test", "--dry-run", "--confirm"],
    ["task", "list"], ["task", "show", "7"],
    ["task", "list", "7", "--status", "unknown"],
    ["task", "failures"], ["task", "log", "7", "T000001", "--tail", "-1"],
    ["task", "rerun", "7", "T000001", "--dry-run", "--confirm"],
    ["task", "rerun", "7", "../T000001", "--dry-run"],
    ["task", "show", "7", "T000001", "--timeout", "nan"],
])
def test_invalid_arguments_emit_json_without_network(runner, offline, capsys, arguments):
    assert runner.main(arguments) == 2
    assert result(capsys)["success"] is False


def test_steps_show_returns_document(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [item_page()]
    assert runner.main(["steps", "show", "7", "42"]) == 0
    assert result(capsys)["data"] == STEPS


def test_steps_set_reads_version_then_patches_document(runner, transport, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    document = copy.deepcopy(STEPS)
    document["steps"][0]["description"] = "replacement"
    source = tmp_path / "steps.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    transport["responses"] = [item_page(), {"item": {**ITEM, "version": 10, "steps": document}}]
    assert runner.main(["steps", "set", "7", "42", "--json", "@" + str(source), "--confirm"]) == 0
    assert result(capsys)["success"] is True
    assert [(method, path) for method, path, kwargs in transport["calls"]] == [
        ("GET", ITEM_PATH), ("PATCH", ITEM_PATH + "/42")]
    # The server stores a ``steps`` value with ``str(raw)`` unless it is already
    # text, which would persist a Python repr the read path cannot parse.
    assert transport["calls"][1][2]["json_body"] == {
        "version": 9, "changes": {"steps": json.dumps(document, ensure_ascii=False)}}


def test_steps_set_conflict_is_explicit_and_never_retries(runner, transport, capsys, monkeypatch):
    from silver_cli.transport import CliError

    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [item_page(), CliError(
        "VERSION_CONFLICT", "Item changed", 5, status=409,
        details={"current_version": 10}, request_id="req-conflict")]
    assert runner.main(["steps", "set", "7", "42", "--json", json.dumps(STEPS), "--confirm"]) == 5
    payload = result(capsys)
    assert payload["success"] is False
    assert payload["error"]["code"] == "VERSION_CONFLICT"
    assert payload["status"] == 409
    assert payload["request_id"] == "req-conflict"
    assert len(transport["calls"]) == 2
    assert transport["calls"][1][2]["json_body"]["version"] == 9


@pytest.mark.parametrize("version", [None, "9", True])
def test_steps_set_refuses_invalid_remote_version(runner, transport, capsys, monkeypatch, version):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [item_page([{**ITEM, "version": version}])]
    assert runner.main(["steps", "set", "7", "42", "--json", json.dumps(STEPS), "--confirm"]) == 7
    assert result(capsys)["error"]["code"] == "INVALID_RESPONSE"
    assert [method for method, path, kwargs in transport["calls"]] == ["GET"]


def test_steps_set_scans_pages_and_uses_target_item_version(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [
        item_page([{**ITEM, "id": 41, "version": 88}], page_size=1, total=2),
        item_page(page=2, page_size=1, total=2), {"item": {**ITEM, "version": 10}}]
    assert runner.main(["steps", "set", "7", "42", "--json", json.dumps(STEPS), "--confirm"]) == 0
    assert result(capsys)["success"] is True
    assert [dict(kwargs["query"])["page"] for method, path, kwargs in transport["calls"][:2]] == ["1", "2"]
    assert transport["calls"][2][:2] == ("PATCH", ITEM_PATH + "/42")
    assert transport["calls"][2][2]["json_body"]["version"] == 9


def test_steps_set_missing_item_does_not_patch(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [item_page([], total=0)]
    assert runner.main(["steps", "set", "7", "42", "--json", json.dumps(STEPS), "--confirm"]) == 2
    assert result(capsys)["error"]["code"] == "NOT_FOUND"
    assert [method for method, path, kwargs in transport["calls"]] == ["GET"]


def test_steps_edit_preserves_other_steps_and_signals(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [item_page(), {"item": {**ITEM, "version": 10}}]
    assert runner.main(["steps", "edit", "7", "42", "--step", "1", "--set",
                        "description=updated", "--set", "enabled=true", "--confirm"]) == 0
    assert result(capsys)["success"] is True
    body = transport["calls"][-1][2]["json_body"]
    assert body["version"] == 9
    document = json.loads(body["changes"]["steps"])
    assert document["input_signals"] == STEPS["input_signals"]
    assert document["expected_signals"] == STEPS["expected_signals"]
    assert document["steps"][0] == {"description": "updated", "enabled": True}
    assert document["steps"][1] == STEPS["steps"][1]


def test_steps_edit_out_of_range_does_not_patch(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [item_page()]
    assert runner.main(["steps", "edit", "7", "42", "--step", "99",
                        "--set", "description=updated", "--confirm"]) == 2
    assert result(capsys)["success"] is False
    assert [method for method, path, kwargs in transport["calls"]] == ["GET"]


def test_task_list_filters_status_locally_and_preserves_truncation(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [{"tasks": [
        {"task_id": "T000001", "status": "failed"},
        {"task_id": "T000002", "status": "passed"}],
        "total": 25, "limit": 2, "truncated": True}]
    assert runner.main(["task", "list", "7", "--status", "failed"]) == 0
    payload = result(capsys)
    assert [task["task_id"] for task in payload["data"]["tasks"]] == ["T000001"]
    assert payload["data"]["truncated"] is True
    assert "status" not in dict(transport["calls"][0][2].get("query", []))


def test_task_failures_includes_verdict_errors_and_only_failed_cases(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    error_event = {"event_type": "error", "message": "runner failed"}
    failed = {"task_id": "T000001", "test_id": "CASE-42", "status": "failed", "result": "FAIL"}
    verdict_error = {"task_id": "T000002", "status": "passed", "result": "ERROR"}
    transport["responses"] = [
        {"tasks": [failed, verdict_error, {"task_id": "T000003", "status": "passed", "result": "PASS"}],
         "total": 3000, "limit": 2000, "truncated": True},
        {"task": {**failed, "message": "execution error", "events": [error_event]}},
        {"available": True, "verdict": "FAIL", "content": "CASE-42 FAIL", "failed_steps": 1},
        {"task": {**verdict_error, "events": []}},
        {"available": False, "verdict": "ERROR", "content": ""},
    ]
    assert runner.main(["task", "failures", "7"]) == 0
    data = result(capsys)["data"]
    assert [task["task_id"] for task in data["failures"]] == ["T000001", "T000002"]
    assert data["failures"][0]["jdgrslt"]["content"] == "CASE-42 FAIL"
    assert data["failures"][0]["errors"] == {"message": "execution error", "events": [error_event]}
    assert data["failures"][1]["jdgrslt"]["available"] is False
    assert data["tasks_scanned"] == 3
    assert data["truncated"] is True
    assert [(method, path) for method, path, kwargs in transport["calls"]] == [
        ("GET", TASK_PATH),
        ("GET", TASK_PATH + "/T000001/detail"), ("GET", TASK_PATH + "/T000001/jdgrslt"),
        ("GET", TASK_PATH + "/T000002/detail"), ("GET", TASK_PATH + "/T000002/jdgrslt")]


@pytest.mark.parametrize(("status", "verdict", "expected_count"), [
    ("failed", "FAIL", 1), ("error", "ERROR", 1), ("passed", "PASS", 0)])
def test_task_failures_selects_one_task_without_project_scan(
        runner, transport, capsys, monkeypatch, status, verdict, expected_count):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [{"task": {"task_id": "T000001", "status": status,
                                        "result": verdict, "events": []}}]
    if expected_count:
        transport["responses"].append({"available": True, "verdict": verdict, "content": "judge output"})
    assert runner.main(["task", "failures", "7", "--task", "T000001"]) == 0
    assert len(result(capsys)["data"]["failures"]) == expected_count
    assert transport["calls"][0][:2] == ("GET", TASK_PATH + "/T000001/detail")
    assert len(transport["calls"]) == 1 + expected_count


def test_task_log_tails_lines_and_preserves_error_details(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    error_event = {"event_type": "error", "message": "error before tail"}
    transport["responses"] = [{"task": {"task_id": "T000001", "status": "failed",
        "message": "license unavailable", "events": [error_event,
            {"event_type": "progress", "message": "50 percent"},
            {"event_type": "log", "message": "first\nsecond"},
            {"event_type": "warning", "message": "last"}]}}]
    assert runner.main(["task", "log", "7", "T000001", "--tail", "2"]) == 0
    data = result(capsys)["data"]
    assert data["log"] == "second\nlast"
    assert data["errors"] == {"message": "license unavailable", "events": [error_event]}
    assert data["events_returned"] == 4
    assert data["possibly_truncated"] is False


def test_task_log_marks_capped_event_window(runner, transport, capsys, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [{"task": {"task_id": "T000001", "events": [
        {"event_type": "log", "message": "line"}] * 5000}}]
    assert runner.main(["task", "log", "7", "T000001", "--tail", "1"]) == 0
    data = result(capsys)["data"]
    assert data["log"] == "line"
    assert data["possibly_truncated"] is True


def test_project_show_stops_after_remote_failure(runner, transport, capsys, monkeypatch):
    from silver_cli.transport import CliError

    monkeypatch.setenv("SILVER_CLI_URL", "https://silver.example.test")
    transport["responses"] = [{"project": {"id": 7}},
        CliError("FORBIDDEN", "No field access", 4, status=403, request_id="req-fields")]
    assert runner.main(["project", "show", "7"]) == 4
    payload = result(capsys)
    assert payload["error"]["code"] == "FORBIDDEN"
    assert payload["request_id"] == "req-fields"
    assert len(transport["calls"]) == 2


@pytest.mark.parametrize("arguments", [[], ["project"], ["project", "list"],
    ["project", "show"], ["project", "create"], ["item"], ["item", "list"],
    ["item", "show"], ["steps"], ["steps", "show"], ["steps", "set"],
    ["steps", "edit"], ["task"], ["task", "list"], ["task", "show"],
    ["task", "failures"], ["task", "log"], ["task", "rerun"]])
def test_help_is_offline_and_includes_examples(runner, offline, capsys, arguments):
    with pytest.raises(SystemExit) as stopped:
        runner.main(arguments + ["--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "usage:" in output.out
    expected_options = {
        ("project", "create"): ("--json", "--confirm", "--dry-run"),
        ("item", "list"): ("--sheet", "--page", "--page-size"),
        ("steps", "set"): ("--json", "--confirm", "--dry-run"),
        ("steps", "edit"): ("--step", "--set", "--confirm", "--dry-run"),
        ("task", "list"): ("--status",),
        ("task", "failures"): ("--task",),
        ("task", "log"): ("--tail",),
        ("task", "rerun"): ("--confirm", "--dry-run"),
    }
    for option in expected_options.get(tuple(arguments), ()):
        assert option in output.out
    if arguments:
        assert "example" in output.out.lower()
    else:
        for command in ("list", "describe", "call", "wait", "project", "item", "steps", "task"):
            assert command in output.out


def test_command_help_without_site_packages():
    completed = subprocess.run([sys.executable, "-S", "-m", "silver_cli", "steps", "set", "--help"],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    for option in ("--json", "--confirm", "--dry-run"):
        assert option in completed.stdout
