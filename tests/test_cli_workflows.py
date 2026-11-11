"""Agent workflow commands: search, Silver runs, reports, models and the UT pipeline."""

from __future__ import annotations

import copy
import json

import pytest

from silver_cli.transport import CliError

P = "/api/v1/projects/7"
UT = P + "/ut"


def envelope(data):
    return {"success": True, "data": data, "error": None, "request_id": "req"}


@pytest.fixture
def cli(monkeypatch, capsys):
    from silver_cli import runner, workflows
    from silver_cli.transport import Client

    monkeypatch.setenv("SILVER_CLI_URL", "http://127.0.0.1:5000")
    for name in ("SILVER_CLI_USERNAME", "SILVER_CLI_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    clock = [0.0]
    monkeypatch.setattr(workflows.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(workflows.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    state = {"calls": [], "responses": []}

    def request(self, method, path, **kwargs):
        state["calls"].append((method, path, kwargs))
        assert state["responses"], f"Unexpected HTTP request: {method} {path}"
        response = state["responses"].pop(0)
        if isinstance(response, Exception):
            raise response
        return envelope(copy.deepcopy(response))

    monkeypatch.setattr(Client, "__init__", lambda self, *args, **kwargs: setattr(self, "timeout", 30))
    monkeypatch.setattr(Client, "request", request)

    def run(*arguments, responses=()):
        state["responses"] = list(responses)
        code = runner.main(list(arguments))
        output = json.loads(capsys.readouterr().out)
        assert not state["responses"], "Expected requests were not sent"
        return code, output, state["calls"]

    run.state = state
    return run


def task(key, status, test_id="TC-1"):
    return {"task_id": key, "test_id": test_id, "status": status, "result": "", "sil_name": "plant",
            "sil_version": "v1", "message": "", "events": []}


def test_project_search_filters_code_name_and_description(cli):
    projects = [{"id": 1, "code": "BRK", "name": "Brake"}, {"id": 2, "code": "X", "description": "brake ecu"},
                {"id": 3, "code": "Y", "name": "Door"}]
    code, out, _ = cli("project", "list", "--search", "BRAKE", responses=[{"projects": projects}])
    assert code == 0 and [p["id"] for p in out["data"]["projects"]] == [1, 2]


def test_task_search_maps_filters_to_cross_project_query(cli):
    code, out, calls = cli("task", "search", "--project", "7", "--q", "TC-1", "--status", "failed", "--mine",
                           responses=[{"tasks": []}])
    assert code == 0
    assert calls[0][:2] == ("GET", "/api/v1/me/tasks")
    assert calls[0][2]["query"] == [("project_id", "7"), ("q", "TC-1"), ("status", "failed"), ("mine", "1")]


@pytest.mark.parametrize(("final", "verdict", "expected"), [("passed", "PASS", 0), ("failed", "FAIL", 8)])
def test_task_run_wait_reports_status_and_verdict(cli, final, verdict, expected):
    code, out, calls = cli(
        "task", "run", "7", "--test-id", "TC-1", "--model", "plant@v1", "--wait", "--confirm",
        responses=[{"created": [{"test_id": "TC-1", "task_id": "T1"}], "missing": [], "errors": [], "duplicates": []},
                   {"task": task("T1", "running")}, {"task": task("T1", final)},
                   {"task": task("T1", final)}, {"available": True, "verdict": verdict, "failed_steps": 0,
                                                 "content": "case TC-1"}])
    assert code == expected
    assert calls[0][1] == P + "/tasks/run-selected"
    assert calls[0][2]["json_body"] == {"model": "plant@v1", "test_ids": ["TC-1"]}
    run = out["data"]["tasks"][0]
    assert (run["status"], run["verdict"], run["jdgrslt"]) == (final, verdict, "case TC-1")
    assert out["data"]["all_passed"] is (expected == 0)


def test_task_wait_timeout_does_not_cancel(cli):
    code, out, calls = cli("task", "wait", "7", "T1", "--max-wait", "10", "--interval", "5",
                           responses=[{"task": task("T1", "queued")}] * 3)
    assert code == 9 and out["error"]["code"] == "WAIT_TIMEOUT"
    assert all(method == "GET" for method, *_ in calls)


def test_task_report_joins_steps_latest_run_and_writes_markdown(cli, tmp_path):
    steps = {"input_signals": [], "expected_signals": [], "steps": [{"description": "press brake"}]}
    items = {"items": [{"id": 1, "case_id": "C1", "test_id": "TC-1", "title": "Brake", "steps": json.dumps(steps)},
                       {"id": 2, "case_id": "TC-2", "steps": None}], "page": 1, "page_size": 500, "total": 2}
    runs = {"tasks": [task("T9", "failed"), task("T1", "passed")], "total": 2, "truncated": False}
    output = tmp_path / "report.md"
    code, out, calls = cli("task", "report", "7", "--output", str(output),
                           responses=[items, runs, {"task": task("T9", "failed")},
                                      {"verdict": "FAIL", "failed_steps": 1, "content": "NG step 1"}])
    assert code == 0
    first, second = out["data"]["cases"]
    assert first["steps"] == steps and first["latest_run"]["task_id"] == "T9"
    assert first["latest_run"]["verdict"] == "FAIL"
    assert second["test_id"] == "TC-2" and second["latest_run"] is None
    text = output.read_text(encoding="utf-8")
    assert "## TC-1 Brake" in text and "press brake" in text and "NG step 1" in text
    assert calls[0][2]["query"] == [("sheet", "test"), ("page", "1"), ("page_size", "500")]


def test_task_report_filters_never_run_cases(cli):
    items = {"items": [{"id": 1, "case_id": "TC-1"}, {"id": 2, "case_id": "TC-2"}],
             "page": 1, "page_size": 500, "total": 2}
    code, out, _ = cli("task", "report", "7", "--status", "not_run", "--no-steps",
                       responses=[items, {"tasks": [task("T1", "passed")], "total": 1}])
    assert [case["test_id"] for case in out["data"]["cases"]] == ["TC-2"]
    assert "steps" not in out["data"]["cases"][0]


TARGETS = [{"id": 5, "function": "Foo", "source_rel": "a/foo.c", "in_index": True},
           {"id": 6, "function": "Bar", "source_rel": "a/bar.c", "in_index": True},
           {"id": 7, "function": "Bar", "source_rel": "b/bar.c", "in_index": True},
           {"id": 8, "function": "Old", "source_rel": "a/old.c", "in_index": False}]


def test_ut_run_resolves_functions_and_waits_for_batch(cli):
    job = {"id": 30, "batch_id": 3, "state": "awaiting_review", "target": {"function": "Foo"}, "coverage": None}
    code, out, calls = cli("ut", "run", "7", "--function", "Foo", "--function", "Bar", "--source", "b/",
                           "--wait", "--confirm",
                           responses=[[{**TARGETS[0], "source_rel": "b/foo.c"}, *TARGETS[1:]], {"id": 3},
                                      [{**job, "state": "generating"}], [job]])
    assert code == 0
    assert calls[1][2]["json_body"] == {"target_ids": [5, 7], "label": ""}
    assert calls[2][2]["query"] == [("batch_id", "3")]
    assert out["data"]["states"] == {"awaiting_review": 1}


@pytest.mark.parametrize("function", ["Bar", "Old", "Missing"])
def test_ut_run_refuses_ambiguous_or_unindexed_function(cli, function):
    code, out, calls = cli("ut", "run", "7", "--function", function, "--confirm", responses=[TARGETS])
    assert code == 2 and out["error"]["code"] == "TARGET_UNRESOLVED"
    assert [method for method, *_ in calls] == ["GET"]


def test_ut_review_pins_the_attempt_it_read(cli):
    code, _, calls = cli("ut", "review", "7", "12", "--decision", "approve", "--note", "ok", "--confirm",
                         responses=[{"id": 12, "current_attempt_id": 44}, {"id": 12, "state": "approved"}])
    assert code == 0
    assert calls[1][1] == UT + "/jobs/12/review"
    assert calls[1][2]["json_body"] == {"decision": "approve", "note": "ok", "expected_attempt_id": 44}


def test_ut_evidence_collects_sections_and_keeps_partial_failures(cli):
    job = {"id": 12, "state": "awaiting_review", "current_attempt_id": 44, "target": {"function": "Foo"},
           "attempts": [{"id": 44, "seq": 1}]}
    code, out, calls = cli("ut", "evidence", "7", "12",
                           responses=[job, {"name": "Foo.csv", "text": "a,b"},
                                      CliError("NOT_FOUND", "no diagnostic", 2, status=404), {"c0": 100}])
    assert code == 0
    data = out["data"]
    assert data["csv"]["text"] == "a,b" and data["coverage"] == {"c0": 100}
    assert data["diagnostic"]["unavailable"] is True
    assert [call[2].get("query") for call in calls[1:3]] == [[("format", "text")], [("format", "diagnostic")]]


def test_ut_revise_uploads_csv_bytes(cli, tmp_path):
    revised = tmp_path / "Foo.csv"
    revised.write_bytes("項目\r\n".encode("cp932"))
    code, _, calls = cli("ut", "revise", "7", "12", "--csv", str(revised), "--note", "fix", "--confirm",
                         responses=[{"id": 12}])
    assert code == 0
    assert calls[0][2]["files"] == [("file", revised)] and calls[0][2]["form"] == [("note", "fix")]


def test_ut_configure_sends_object_unchanged(cli, tmp_path):
    config = {"index_csv": "D:/p/index.csv", "product_root": "D:/p", "max_repair_rounds": 3}
    source = tmp_path / "ut.json"
    source.write_text(json.dumps(config), encoding="utf-8")
    code, _, calls = cli("ut", "configure", "7", "--json", "@" + str(source), "--confirm", responses=[config])
    assert code == 0 and calls[0][:2] == ("PUT", UT + "/config") and calls[0][2]["json_body"] == config


@pytest.mark.parametrize("arguments", [
    ["task", "run", "7", "--test-id", "TC-1"],
    ["task", "cancel", "7", "T1"],
    ["model", "use", "7", "--name", "plant"],
    ["ut", "configure", "7", "--json", "{}"],
    ["ut", "import-targets", "7"],
    ["ut", "run", "7", "--target-id", "5"],
    ["ut", "retry", "7", "12"],
    ["ut", "stop", "7", "12"],
    ["ut", "stop-batch", "7", "3"],
    ["ut", "review", "7", "12", "--decision", "reject"],
])
def test_new_writes_need_confirm_and_dry_run_is_offline(cli, arguments):
    code, out, calls = cli(*arguments)
    assert code == 2 and out["error"]["code"] == "CONFIRM_REQUIRED" and calls == []
    code, out, calls = cli(*arguments, "--dry-run")
    assert code == 0 and out["data"]["dry_run"] is True and calls == []


STEP_DOC = {"input_signals": [{"name": "speed"}], "expected_signals": [{"name": "ready"}],
            "steps": [{"description": "a"}, {"description": "b"}, {"description": "c"}]}


def items_page(*rows):
    return {"items": list(rows), "page": 1, "page_size": 500, "total": len(rows)}


def row(item_id, test_id, steps=STEP_DOC):
    return {"id": item_id, "case_id": "C" + str(item_id), "test_id": test_id, "version": 4, "steps": steps}


@pytest.mark.parametrize(("arguments", "expected"), [
    (["add", "--json", '{"description": "new"}', "--at", "2"], ["a", "new", "b", "c"]),
    (["add", "--json", '{"description": "new"}'], ["a", "b", "c", "new"]),
    (["remove", "--step", "2"], ["a", "c"]),
    (["move", "--step", "3", "--to", "1"], ["c", "a", "b"]),
])
def test_steps_structure_edits_by_test_id_keep_signals_and_pin_version(cli, arguments, expected):
    code, _, calls = cli("steps", arguments[0], "7", "--test-id", "TC-2", *arguments[1:], "--confirm",
                         responses=[items_page(row(41, "TC-1"), row(42, "TC-2")), {"item": {"id": 42}}])
    assert code == 0
    assert calls[0][2]["query"][0] == ("sheet", "test")
    method, path, kwargs = calls[1]
    assert (method, path) == ("PATCH", P + "/items/42")
    document = kwargs["json_body"]["changes"]["steps"]
    assert kwargs["json_body"]["version"] == 4
    assert [step["description"] for step in document["steps"]] == expected
    assert document["input_signals"] == STEP_DOC["input_signals"]


def test_steps_show_by_test_id_falls_back_to_case_id(cli):
    code, out, _ = cli("steps", "show", "7", "--test-id", "C9",
                       responses=[items_page({"id": 9, "case_id": "C9", "version": 1, "steps": STEP_DOC})])
    assert code == 0 and out["data"] == STEP_DOC


@pytest.mark.parametrize(("arguments", "rows", "error"), [
    (["remove", "--step", "4"], [row(42, "TC-2")], "STEP_NOT_FOUND"),
    (["move", "--step", "1", "--to", "4"], [row(42, "TC-2")], "STEP_NOT_FOUND"),
    (["add", "--json", "{}", "--at", "5"], [row(42, "TC-2")], "STEP_NOT_FOUND"),
    (["remove", "--step", "1"], [row(42, "TC-2"), row(43, "TC-2")], "AMBIGUOUS_TEST_ID"),
    (["remove", "--step", "1"], [row(41, "TC-1")], "NOT_FOUND"),
])
def test_steps_structure_edit_refusals_never_patch(cli, arguments, rows, error):
    code, out, calls = cli("steps", arguments[0], "7", "--test-id", "TC-2", *arguments[1:], "--confirm",
                           responses=[items_page(*rows)])
    assert code == 2 and out["error"]["code"] == error
    assert [method for method, *_ in calls] == ["GET"]


@pytest.mark.parametrize("target", [[], ["42", "--test-id", "TC-2"]])
def test_steps_need_exactly_one_item_reference(cli, target):
    code, out, calls = cli("steps", "show", "7", *target)
    assert code == 2 and out["error"]["code"] == "ARGUMENT_ERROR" and calls == []


def test_steps_add_dry_run_is_offline(cli):
    code, out, calls = cli("steps", "add", "7", "--test-id", "TC-2", "--json", '{"description": "x"}', "--dry-run")
    assert code == 0 and calls == []
    assert out["data"]["test_id"] == "TC-2" and out["data"]["new_step"] == {"description": "x"}
    assert out["data"]["requests"][1]["path"] == P + "/items/<item resolved from --test-id>"
