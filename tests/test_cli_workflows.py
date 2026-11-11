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
    assert run["model"] == "plant@v1" and run["error_events"] == []
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


CASE_ITEMS = items_page(row(1, "TC-1"), row(2, "TC-2"), row(3, "TC-3"), {"id": 4, "case_id": "", "test_id": ""})
CASE_RUNS = {"tasks": [{**task("T3", "failed", "TC-2"), "result": "FAIL"}, {**task("T2", "passed", "TC-1"), "result": "PASS"},
                       {**task("T1", "passed", "TC-2"), "result": "PASS"}], "total": 3, "truncated": False}


@pytest.mark.parametrize(("arguments", "expected"), [
    ([], [("TC-1", "passed"), ("TC-2", "failed"), ("TC-3", "not_run")]),
    (["--failed"], [("TC-2", "failed")]),
    (["--status", "not_run"], [("TC-3", "not_run")]),
])
def test_case_list_gives_test_ids_with_latest_result(cli, arguments, expected):
    code, out, calls = cli("case", "list", "7", *arguments, responses=[CASE_ITEMS, CASE_RUNS])
    assert code == 0
    assert [(case["test_id"], case["status"]) for case in out["data"]["cases"]] == expected
    assert all(method == "GET" for method, *_ in calls) and len(calls) == 2


FIELDS = {"fields": [
    {"field_key": "test_id", "display_name": "テストID", "sheet": "test"},
    {"field_key": "purpose", "display_name": "テスト目的", "sheet": "test"},
    {"field_key": "steps", "display_name": "手順", "sheet": "test"},
    {"field_key": "const_name", "display_name": "识别子名", "sheet": "const"},
    {"field_key": "const_value", "display_name": "值", "sheet": "const"},
    {"field_key": "io_name", "display_name": "名称", "sheet": "io"},
    {"field_key": "lib_func", "display_name": "函数标识", "sheet": "lib"},
    {"field_key": "lib_value", "display_name": "目的", "sheet": "lib"},
    {"field_key": "lib_stb", "display_name": "测试手顺", "sheet": "lib", "data_type": "steps"},
]}
PURPOSE_ITEMS = items_page(row(1, "TC-1"), {**row(2, "TC-2"), "purpose": "brake check"}, row(3, "TC-3"))


def test_case_show_returns_info_steps_and_latest_verdict(cli):
    code, out, _ = cli("case", "show", "7", "TC-2",
                       responses=[FIELDS, PURPOSE_ITEMS, CASE_RUNS, {"task": task("T3", "failed", "TC-2")},
                                  {"verdict": "FAIL", "failed_steps": 1, "content": "NG"}])
    assert code == 0
    data = out["data"]
    assert data["test_id"] == "TC-2" and data["steps"] == STEP_DOC and data["version"] == 4
    assert data["info"] == [{"field": "test_id", "name": "テストID", "value": "TC-2"},
                            {"field": "purpose", "name": "テスト目的", "value": "brake check"}]
    assert data["latest_run"]["task_id"] == "T3" and data["latest_run"]["verdict"] == "FAIL"
    assert "context" not in data


def test_case_show_context_lists_const_io_and_lib(cli):
    const = items_page({"id": 50, "const_name": "SPEED_MAX", "const_value": "120"})
    io = items_page({"id": 60, "io_name": "speed", "io_note": "unlisted field is dropped"})
    lib = items_page({"id": 70, "lib_func": "Init", "lib_value": "", "lib_stb": json.dumps(STEP_DOC)})
    code, out, calls = cli("case", "show", "7", "TC-1", "--context",
                           responses=[FIELDS, PURPOSE_ITEMS, {"tasks": [], "total": 0}, const, io, lib])
    assert code == 0
    context = out["data"]["context"]
    assert context["const"]["rows"] == [{"item_id": 50, "const_name": "SPEED_MAX", "const_value": "120"}]
    assert context["const"]["fields"] == {"const_name": "识别子名", "const_value": "值"}
    assert context["io"]["rows"] == [{"item_id": 60, "io_name": "speed"}]
    assert context["lib"]["rows"] == [{"item_id": 70, "lib_func": "Init", "lib_stb": STEP_DOC}]
    assert [call[2]["query"][0] for call in calls[3:]] == [("sheet", "const"), ("sheet", "io"), ("sheet", "lib")]


def test_project_context_selects_sheets(cli):
    code, out, _ = cli("project", "context", "7", "--sheet", "lib",
                       responses=[FIELDS, items_page({"id": 70, "lib_func": "Init"})])
    assert code == 0 and list(out["data"]) == ["lib"]


def test_case_show_unknown_test_id(cli):
    code, out, _ = cli("case", "show", "7", "TC-9", responses=[FIELDS, CASE_ITEMS])
    assert code == 2 and out["error"]["code"] == "NOT_FOUND"


PROJECTS = {"projects": [{"id": 7, "code": "BRK"}, {"id": 8, "code": "door"}, {"id": 9, "code": "Door"}]}


def test_project_code_resolves_once_and_routes_by_id(cli):
    code, out, calls = cli("case", "show", "brk", "TC-2",
                           responses=[PROJECTS, FIELDS, CASE_ITEMS, CASE_RUNS, {"task": task("T3", "failed", "TC-2")},
                                      {"verdict": "FAIL", "content": ""}])
    assert code == 0
    assert calls[0][1] == "/api/v1/projects"
    assert [path for _, path, _ in calls[1:]] == [P + "/fields", P + "/items", P + "/tasks/latest", P + "/tasks/T3/detail",
                                                  P + "/tasks/T3/jdgrslt"]


@pytest.mark.parametrize(("ref", "error"), [("NOPE", "PROJECT_NOT_FOUND"), ("DOOR", "AMBIGUOUS_PROJECT")])
def test_unknown_or_ambiguous_project_code_stops(cli, ref, error):
    code, out, calls = cli("case", "list", ref, responses=[PROJECTS])
    assert code == 2 and out["error"]["code"] == error and len(calls) == 1


def test_exact_code_wins_over_case_insensitive_matches(cli):
    code, _, calls = cli("ut", "jobs", "Door", responses=[PROJECTS, []])
    assert code == 0 and calls[1][1] == "/api/v1/projects/9/ut/jobs"


def test_project_code_dry_run_stays_offline(cli):
    code, out, calls = cli("task", "run", "BRK", "--test-id", "TC-1", "--dry-run")
    assert code == 0 and calls == []
    assert out["data"]["requests"][0]["path"] == "/api/v1/projects/<id of BRK>/tasks/run-selected"


def test_task_search_accepts_project_code(cli):
    code, _, calls = cli("task", "search", "--project", "BRK", responses=[PROJECTS, {"tasks": []}])
    assert code == 0 and calls[1][2]["query"] == [("project_id", "7")]


def test_case_set_patches_fields_with_current_version(cli):
    code, _, calls = cli("case", "set", "7", "TC-2", "--set", "purpose=new purpose", "priority=1", "--confirm",
                         responses=[CASE_ITEMS, {"item": {"id": 2}}])
    assert code == 0
    assert calls[1][:2] == ("PATCH", P + "/items/2")
    assert calls[1][2]["json_body"] == {"version": 4, "changes": {"purpose": "new purpose", "priority": 1}}


def test_case_set_refuses_steps(cli):
    code, out, calls = cli("case", "set", "7", "TC-2", "--set", "steps={}", "--confirm")
    assert code == 2 and out["error"]["code"] == "FIELD_NOT_ALLOWED" and calls == []


def test_case_add_inserts_below_anchor_and_refuses_duplicates(cli):
    code, _, calls = cli("case", "add", "7", "--json", '{"test_id": "TC-9", "purpose": "p"}', "--after", "TC-1",
                         "--confirm", responses=[CASE_ITEMS, CASE_RUNS, CASE_ITEMS, {"item": {"id": 99}}])
    assert code == 0
    assert calls[3][:2] == ("POST", P + "/items")
    assert calls[3][2]["json_body"] == {"sheet": "test", "values": {"test_id": "TC-9", "purpose": "p"},
                                        "anchor_id": 1, "place": "below"}
    code, out, calls = cli("case", "add", "7", "--json", '{"test_id": "TC-1"}', "--confirm",
                           responses=[CASE_ITEMS, CASE_RUNS])
    assert code == 2 and out["error"]["code"] == "DUPLICATE" and all(m == "GET" for m, *_ in calls[-2:])


def test_case_copy_drops_results_and_sets_new_id(cli):
    source = {**row(2, "TC-2"), "purpose": "p", "result": "PASS", "exec_date": "2026-01-01", "log": "T3"}
    page = items_page(row(1, "TC-1"), source)
    code, _, calls = cli("case", "copy", "7", "TC-2", "--as", "TC-9", "--confirm",
                         responses=[page, page, {"item": {"id": 99}}])
    assert code == 0
    body = calls[2][2]["json_body"]
    assert body["anchor_id"] == 2 and body["place"] == "below"
    values = body["values"]
    assert values["test_id"] == values["case_id"] == "TC-9" and values["purpose"] == "p"
    assert values["steps"] == STEP_DOC
    assert not {"result", "exec_date", "log", "id", "version"} & set(values)


def test_case_copy_refuses_existing_new_id(cli):
    code, out, calls = cli("case", "copy", "7", "TC-2", "--as", "TC-1", "--confirm", responses=[CASE_ITEMS])
    assert code == 2 and out["error"]["code"] == "DUPLICATE" and len(calls) == 1


def test_case_delete_soft_deletes_resolved_item(cli):
    code, out, calls = cli("case", "delete", "7", "TC-3", "--confirm", responses=[CASE_ITEMS, {"deleted": True}])
    assert code == 0 and calls[1][:2] == ("DELETE", P + "/items/3")
    assert out["data"]["test_id"] == "TC-3"


@pytest.mark.parametrize(("sheet", "key", "rows", "expected"), [
    ("const", "SPEED_MAX", [{"id": 50, "const_name": "SPEED_MAX", "version": 2}], 50),
    ("io", "speed", [{"id": 60, "io_name": "speed", "version": 2}], 60),
    ("lib", "Init", [{"id": 70, "lib_func": "Init", "version": 2}, {"id": 71, "lib_func": "Wait", "version": 2}], 70),
])
def test_row_set_finds_row_by_business_key(cli, sheet, key, rows, expected):
    code, _, calls = cli("row", "set", "7", "--sheet", sheet, "--key", key, "--set", "x=1", "--confirm",
                         responses=[items_page(*rows), {"item": {}}])
    assert code == 0
    assert calls[0][2]["query"][0] == ("sheet", sheet)
    assert calls[1][:2] == ("PATCH", f"{P}/items/{expected}")
    assert calls[1][2]["json_body"] == {"version": 2, "changes": {"x": 1}}


def test_row_set_refuses_duplicate_keys(cli):
    rows = [{"id": 50, "const_name": "A", "version": 1}, {"id": 51, "const_name": "A", "version": 1}]
    code, out, calls = cli("row", "set", "7", "--sheet", "const", "--key", "A", "--set", "x=1", "--confirm",
                           responses=[items_page(*rows)])
    assert code == 2 and out["error"]["code"] == "AMBIGUOUS_KEY" and len(calls) == 1


@pytest.mark.parametrize(("flag", "expected"), [("--failed", ["TC-2"]), ("--not-run", ["TC-3"]),
                                                ("--all", ["TC-1", "TC-2", "TC-3"])])
def test_task_run_batch_selection(cli, flag, expected):
    code, _, calls = cli("task", "run", "7", flag, "--confirm",
                         responses=[CASE_ITEMS, CASE_RUNS, {"created": [], "missing": [], "errors": [], "duplicates": []}])
    assert code == 0 and calls[2][2]["json_body"]["test_ids"] == expected


def test_task_run_needs_exactly_one_selection(cli):
    for arguments in ([], ["--test-id", "TC-1", "--failed"]):
        code, out, calls = cli("task", "run", "7", *arguments, "--confirm")
        assert code == 2 and out["error"]["code"] == "ARGUMENT_ERROR" and calls == []


def test_run_outcomes_expose_failed_checks_from_structured_jdgrslt(cli):
    steps = [{"step": 1, "result": "PASS", "checks": [{"ok": True, "target": "a"}]},
             {"step": 2, "result": "FAIL", "checks": [{"ok": True, "target": "b"},
                                                      {"ok": False, "target": "c", "expected": "1", "observed": "0"}]}]
    code, out, _ = cli("task", "wait", "7", "T1",
                       responses=[{"task": task("T1", "failed")}, {"task": task("T1", "failed")},
                                  {"verdict": "FAIL", "content": "", "steps": steps}])
    assert code == 8
    run = out["data"]["tasks"][0]
    assert run["step_results"] == steps
    assert run["failed_checks"] == [{"step": 2, "ok": False, "target": "c", "expected": "1", "observed": "0"}]


@pytest.mark.parametrize("arguments", [
    ["case", "set", "7", "TC-1", "--set", "purpose=x"],
    ["case", "add", "7", "--json", '{"test_id": "TC-9"}'],
    ["case", "copy", "7", "TC-1", "--as", "TC-9"],
    ["case", "delete", "7", "TC-1"],
    ["row", "set", "7", "--sheet", "const", "--key", "A", "--set", "x=1"],
    ["task", "run", "7", "--failed"],
])
def test_edit_commands_need_confirm_and_dry_run_offline(cli, arguments):
    code, out, calls = cli(*arguments)
    assert code == 2 and out["error"]["code"] == "CONFIRM_REQUIRED" and calls == []
    code, out, calls = cli(*arguments, "--dry-run")
    assert code == 0 and out["data"]["dry_run"] is True and calls == []


def test_token_create_writes_secret_only_to_new_file(cli, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SILVER_CLI_USERNAME", "agent")
    monkeypatch.setenv("SILVER_CLI_PASSWORD", "pw")
    from silver_cli.transport import Client
    monkeypatch.setattr(Client, "login", lambda self, username, password: None)
    target = tmp_path / "agent.token"
    code, out, calls = cli("token", "create", "--name", "ci", "--days", "30", "--output", str(target), "--confirm",
                           responses=[{"token": "slv_secret_value", "info": {"id": 3, "prefix": "slv_secret_v"}}])
    assert code == 0 and calls[0][:2] == ("POST", "/api/v1/me/tokens")
    assert calls[0][2]["json_body"] == {"name": "ci", "expires_days": 30}
    assert target.read_text(encoding="utf-8") == "slv_secret_value\n"
    assert "slv_secret_value" not in json.dumps(out)


def test_token_and_password_together_are_refused(cli, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_TOKEN", "slv_x")
    monkeypatch.setenv("SILVER_CLI_USERNAME", "agent")
    monkeypatch.setenv("SILVER_CLI_PASSWORD", "pw")
    code, out, calls = cli("project", "list")
    assert code == 2 and out["error"]["code"] == "CREDENTIALS" and calls == []


def test_token_requests_send_bearer_without_login_or_csrf(monkeypatch, capsys):
    from silver_cli import runner
    from silver_cli.transport import Client

    monkeypatch.setenv("SILVER_CLI_URL", "http://127.0.0.1:5000")
    monkeypatch.setenv("SILVER_CLI_TOKEN", "slv_token_value")
    for name in ("SILVER_CLI_USERNAME", "SILVER_CLI_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Client, "login", lambda *a: pytest.fail("token auth must not log in"))
    seen = []

    class Response:
        status = 200
        headers = type("H", (), {"get_content_type": lambda self: "application/json"})()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(Client, "_open", lambda self, request: seen.append(request) or Response())
    monkeypatch.setattr("silver_cli.transport._json", lambda response: envelope({"deleted": True}))
    code = runner.main(["call", "items.delete_item", "--param", "project_id=7", "--param", "item_id=3", "--confirm"])
    output = capsys.readouterr().out
    assert code == 0 and "slv_token_value" not in output
    assert seen[0].get_header("Authorization") == "Bearer slv_token_value"
    assert seen[0].get_header("X-csrf-token") is None
