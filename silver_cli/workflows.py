"""Agent workflows: search, Silver runs, reports, models and the unit-test pipeline.

Every command composes existing HTTP operations through the same transport,
login, CSRF and permission checks as ``call``. Writes follow the shared
contract: ``--dry-run`` sends nothing, ``--confirm`` is required otherwise.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from .transport import CliError

# Silver task execution states (app.models.task.TaskStatus).
TASK_DONE = ("passed", "failed", "cancelled")
TASK_PENDING = ("queued", "running")
# Unit-test job states (app.models.ut_pipeline.UtJob).
UT_ACTIVE = ("queued", "generating", "executing", "repairing")
UT_STATES = UT_ACTIVE + ("awaiting_review", "needs_human", "blocked", "error",
                         "approved", "rejected", "stopped")
UT_CONFIG_FIELDS = (
    "manifest_document: ProjectManifest object; validated by ut-agent and stored by the server "
    "(mutually exclusive with manifest_path)",
    "manifest_path: server path of an existing ProjectManifest",
    "index_csv: server path of the project index CSV (required)",
    "product_root: server path of the product source root (required)",
    "max_repair_rounds: integer 0-20",
    "stub_mode: deterministic or llm",
    "stub_max_rounds: integer 1-5 (llm stub mode)",
    "stub_model, repair_model: model names, at most 128 characters",
    "host_clang, host_llvm_cov, host_llvm_profdata: host tool paths; must not be empty when given",
    "host_profile_runtime: host profile runtime path",
)


def _project_ref(value):
    from .commands import _project_ref as parse
    return parse(value)


def _positive_int(value):
    from .commands import _positive_int as parse
    return parse(value)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register(command, group):
    """Add workflow commands; ``command``/``group`` come from commands.register."""
    from .runner import _positive

    def waiting(parser, default):
        parser.add_argument("--interval", type=_positive, default=5.0,
                            help="Polling interval in seconds (default: 5).")
        parser.add_argument("--max-wait", type=_positive, default=default,
                            help=f"Maximum wait in seconds (default: {default:g}); the remote work is never cancelled.")

    search = command("task", "search", "Search runs across every visible project by task key, test id or name (me.me_tasks). Unit-test jobs matching the same filters are returned in ut_jobs; --limit applies to runs and jobs together, newest first.",
                     "search --q TC-1 --status failed", project=False, handler=task_search)
    search.add_argument("--project", type=_project_ref, help="Limit to one project ID or project code.")
    search.add_argument("--q", help="Substring of task key, test id or task name.")
    search.add_argument("--status", choices=TASK_DONE + TASK_PENDING, help="Execution status.")
    search.add_argument("--mine", action="store_true", help="Only runs submitted by the logged-in user.")
    search.add_argument("--limit", type=_positive_int, help="Maximum runs (server default 200).")

    run = command("task", "run", "Queue test rows for Silver execution with a saved model; runner inputs are generated from the project's test/lib/const rows.",
                  "run 1 --test-id TC-1 --test-id TC-2 --model plant@v1 --confirm", write=True, handler=task_run)
    run.add_argument("--test-id", dest="test_ids", action="append",
                     help="Test id of a test-sheet row (its test_id, else case_id); repeat as needed.")
    selection = run.add_mutually_exclusive_group()
    selection.add_argument("--failed", action="store_true",
                           help="Every case whose latest run failed, was cancelled, or has a FAIL/ERROR result.")
    selection.add_argument("--not-run", action="store_true", help="Every case that has never run.")
    selection.add_argument("--all", action="store_true", help="Every test-sheet case.")
    run.add_argument("--model", default="", help="Model reference NAME@VERSION; default: the project's current model.")
    run.add_argument("--wait", action="store_true", help="Wait for the queued runs and report status and verdicts.")
    waiting(run, 1800.0)

    wait = command("task", "wait", "Wait until runs finish, then report status, judge verdict and errors. Exit 8 when any run did not pass.",
                   "wait 1 T000001 T000002", handler=task_wait)
    wait.add_argument("tids", nargs="+", metavar="TASK_KEY", help="Task keys such as T000001.")
    waiting(wait, 1800.0)

    command("task", "cancel", "Request cancellation of a queued or running task.", "cancel 1 T000001 --confirm",
            task=True, write=True, handler=task_simple("tasks.cancel_project_task"))
    download = command("task", "download", "Download a task's result archive to a new file.",
                       "download 1 T000001 --output T000001.zip", task=True, handler=task_download)
    download.add_argument("--output", type=Path, required=True, help="New file in an existing directory.")

    report = command("task", "report", "Report each test case's steps with its latest run status, judge verdict and errors.",
                     "report 1 --test-id TC-1 --output report.md", handler=task_report)
    report.add_argument("--test-id", dest="test_ids", action="append", help="Only these test ids; repeat as needed.")
    report.add_argument("--status", choices=TASK_DONE + TASK_PENDING + ("not_run",),
                        help="Only cases whose latest run has this status (not_run: never run).")
    report.add_argument("--no-steps", action="store_true", help="Omit steps documents.")
    report.add_argument("--output", type=Path, help="Also write the report to this new file (.md for Markdown, otherwise JSON).")

    group("case", "List a project's test cases by test id with their latest result, or show one case.",
          "case list 1 --failed")
    cases = command("case", "list", "List every test-sheet case: test id, item id, title and latest run status/result.",
                    "list 1 --failed", handler=case_list)
    cases.add_argument("--status", choices=TASK_DONE + TASK_PENDING + ("not_run",),
                       help="Only cases whose latest run has this status (not_run: never run).")
    cases.add_argument("--failed", action="store_true",
                       help="Only cases whose latest run failed, was cancelled, or has a FAIL/ERROR result.")
    show = command("case", "show", "Show one case: every filled test-row field with its display name (purpose etc.), "
                   "steps, and the latest run with verdict, jdgrslt and errors.",
                   "show 1 TC-1 --context", handler=case_show)
    show.add_argument("test_id", help="Test id from case list.")
    show.add_argument("--context", action="store_true",
                      help="Also include every row of the project's const, io and lib sheets used to generate the run.")
    context = command("project", "context", "List every row of the project's const, io and lib sheets with field display names.",
                      "context 1 --sheet const", handler=project_context)
    context.add_argument("--sheet", action="append", choices=CONTEXT_SHEETS,
                         help="Only this sheet; repeat as needed (default: const, io and lib).")

    setting = command("case", "set", "Change fields of one case (purpose, environment, parameter ...) under the item version check. "
                      "Use the steps commands for steps.", "set 1 TC-1 --set purpose=New --confirm",
                      write=True, handler=case_set)
    setting.add_argument("test_id", help="Test id from case list.")
    setting.add_argument("--set", dest="assignments", action="append", nargs="+", required=True, metavar="FIELD=VALUE",
                         help="Field keys as shown by case show; values parse as JSON when valid, otherwise as text.")
    adding = command("case", "add", "Create a test-sheet case from a JSON object of field values (test_id, test_name, purpose, steps ...).",
                     "add 1 --json @case.json --after TC-1 --confirm", write=True, handler=case_add)
    adding.add_argument("--json", dest="json_input", required=True, metavar="JSON|@FILE|@-",
                        help="Field values; include test_id. See case show for field keys.")
    adding.add_argument("--after", help="Insert below the case with this test id (default: at the end).")
    copying = command("case", "copy", "Copy a case under a new test id, directly below it; run results are not copied.",
                      "copy 1 TC-1 --as TC-9 --confirm", write=True, handler=case_copy)
    copying.add_argument("test_id", help="Source test id.")
    copying.add_argument("--as", dest="new_id", required=True, help="Test id of the copy; must not exist yet.")
    deleting = command("case", "delete", "Move a case to the recycle bin (restorable).", "delete 1 TC-9 --confirm",
                       write=True, handler=case_delete)
    deleting.add_argument("test_id", help="Test id from case list.")

    group("row", "Change a const, io or lib row found by its name.", "row set 1 --sheet const --key SPEED_MAX --set const_value=130")
    row = command("row", "set", "Change fields of one const, io or lib row found by const_name, io_name or lib_func, "
                  "under the item version check.", "set 1 --sheet const --key SPEED_MAX --set const_value=130 --confirm",
                  write=True, handler=row_set)
    row.add_argument("--sheet", choices=CONTEXT_SHEETS, required=True)
    row.add_argument("--key", required=True, help="const_name, io_name or lib_func of the row (see project context).")
    row.add_argument("--set", dest="assignments", action="append", nargs="+", required=True, metavar="FIELD=VALUE",
                     help="Field keys as shown by project context; values parse as JSON when valid, otherwise as text.")

    group("model", "List, upload, register or select project models.", "model list 1")
    command("model", "list", "List the project's saved models and versions.", "list 1",
            handler=simple("models.list_project_models"))
    upload = command("model", "upload", "Upload a compiled model (dll, sbs, optional pdb and extra files) as a new saved version.",
                     "upload 1 --name plant --version v1 --dll plant.dll --sbs plant.sbs --confirm",
                     write=True, handler=model_upload)
    upload.add_argument("--name", required=True)
    upload.add_argument("--version", required=True)
    upload.add_argument("--note", default="", help="Version note.")
    upload.add_argument("--dll", type=Path, required=True)
    upload.add_argument("--sbs", type=Path, required=True)
    upload.add_argument("--pdb", type=Path)
    upload.add_argument("--extra", type=Path, action="append", default=[], help="Additional dependency file; repeat as needed.")
    use = command("model", "use", "Make a saved model the project's current model.", "use 1 --name plant --confirm",
                  write=True, handler=model_use)
    use.add_argument("--name", required=True)
    use.add_argument("--model-id", type=_positive_int, help="Exact saved model id when a name has several versions.")

    group("ut", "Unit-test pipeline: configure, import targets, generate, inspect evidence, revise and review.", "ut jobs 1")
    command("ut", "options", "List the installed ut-agent manifest choices (pointer sizes, build profiles, policies).",
            "options 1", handler=simple("ut_pipeline.manifest_options"))
    command("ut", "config", "Show the project's unit-test configuration, including the stored manifest document.",
            "config 1", handler=simple("ut_pipeline.get_config"))
    configure = command("ut", "configure", "Save configuration fields; omitted fields keep their values. Fields: "
                        + "; ".join(UT_CONFIG_FIELDS) + ".",
                        "configure 1 --json @ut-config.json --confirm", write=True, handler=ut_configure)
    configure.add_argument("--json", dest="json_input", required=True, metavar="JSON|@FILE|@-",
                           help="Configuration object; see this command's description for fields.")
    validate = command("ut", "validate-manifest", "Validate a ProjectManifest document without saving it.",
                       "validate-manifest 1 --json @manifest.json", handler=ut_validate)
    validate.add_argument("--json", dest="json_input", required=True, metavar="JSON|@FILE|@-",
                          help="The manifest document itself.")
    paths = command("ut", "paths", "Browse server paths for configuration values.", "paths 1 --path D:/product",
                    handler=ut_paths)
    paths.add_argument("--path", default="")
    paths.add_argument("--kind", default="directory", help="directory or file (server-validated).")
    command("ut", "import-targets", "Re-read the project index into the target list; vanished rows are flagged, not deleted.",
            "import-targets 1 --confirm", write=True, handler=ut_import)
    targets = command("ut", "targets", "List target functions with their latest job.", "targets 1 --function Foo",
                      handler=ut_targets)
    targets.add_argument("--function", action="append", help="Exact function name; repeat as needed.")
    targets.add_argument("--source", help="Substring of the source path.")
    targets.add_argument("--all", action="store_true", help="Include rows no longer in the index.")
    generate = command("ut", "run", "Create a batch for the given functions or target ids and start generation.",
                       "run 1 --function Foo --function Bar --wait --confirm", write=True, handler=ut_run)
    generate.add_argument("--function", action="append", default=[], help="Function name; repeat as needed.")
    generate.add_argument("--source", help="Disambiguate functions by a source path substring.")
    generate.add_argument("--target-id", type=_positive_int, action="append", default=[], help="Target id; repeat as needed.")
    generate.add_argument("--label", default="")
    generate.add_argument("--wait", action="store_true", help="Wait until no job of the batch is active.")
    waiting(generate, 3600.0)
    uwait = command("ut", "wait", "Wait until the batch or jobs leave the active states, then summarize them.",
                    "wait 1 --batch 3", handler=ut_wait)
    uwait.add_argument("--batch", type=_positive_int)
    uwait.add_argument("--job", type=_positive_int, action="append", default=[])
    waiting(uwait, 3600.0)
    jobs = command("ut", "jobs", "List jobs with state, verdict, gate and coverage digest.", "jobs 1 --state awaiting_review",
                   handler=ut_jobs)
    jobs.add_argument("--state", choices=UT_STATES)
    jobs.add_argument("--batch", type=_positive_int)
    command("ut", "batches", "List batches.", "batches 1", handler=simple("ut_pipeline.list_batches"))
    command("ut", "job", "Show one job with all attempts, checks and timing.", "job 1 12", job=True,
            handler=ut_job_simple("ut_pipeline.job_detail"))
    evidence = command("ut", "evidence", "Collect one attempt's evidence as JSON: candidate CSV text, diagnostic and coverage.",
                       "evidence 1 12", job=True, handler=ut_evidence)
    evidence.add_argument("--attempt", type=_positive_int, help="Attempt id; default: the job's current attempt.")
    revise = command("ut", "revise", "Submit a revised candidate CSV (sent byte for byte) and re-run machine checks.",
                     "revise 1 12 --csv Foo.csv --note fix --confirm", job=True, write=True, handler=ut_revise)
    revise.add_argument("--csv", type=Path, required=True)
    revise.add_argument("--note", default="")
    command("ut", "retry", "Retry a job from generation.", "retry 1 12 --confirm", job=True, write=True,
            handler=ut_job_simple("ut_pipeline.retry"))
    command("ut", "stop", "Stop one job.", "stop 1 12 --confirm", job=True, write=True,
            handler=ut_job_simple("ut_pipeline.stop_job"))
    stop_batch = command("ut", "stop-batch", "Stop every active job of a batch.", "stop-batch 1 3 --confirm",
                         write=True, handler=ut_stop_batch)
    stop_batch.add_argument("batch", type=_positive_int)
    review = command("ut", "review", "Record a human review decision for the job's current candidate. "
                     "The decision is attributed to the logged-in account; agents must not approve without a person's consent.",
                     "review 1 12 --decision approve --note ok --confirm", job=True, write=True, handler=ut_review)
    review.add_argument("--decision", choices=("approve", "reject"), required=True)
    review.add_argument("--note", default="", help="Required by the server to approve a candidate that failed machine checks.")
    export = command("ut", "export", "Download approved CSVs as a zip archive.", "export 1 --output approved.zip",
                     handler=ut_export)
    export.add_argument("--batch", type=_positive_int)
    export.add_argument("--output", type=Path, required=True, help="New file in an existing directory.")
    command("ut", "stats", "Show repair statistics.", "stats 1", handler=simple("ut_pipeline.repair_stats"))


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _call(client, name, params=None, *, query=None, body=None, **kwargs):
    from .commands import _request

    return _request(client, name, params, query=query, body=body, **kwargs)


def _params(args, **extra):
    return {"project_id": args.pid, **extra}


def _gate(args, plan, **details):
    """Return a dry-run envelope, raise without --confirm, or None to proceed."""
    from .commands import _request_spec
    from .runner import _ok

    if args.dry_run:
        requests = [_request_spec(name, params, query=query, body=body) for name, params, query, body in plan]
        return _ok({"dry_run": True, "requests": requests, **details})
    if not args.confirm:
        raise CliError("CONFIRM_REQUIRED", "Mutating commands require --confirm; inspect --dry-run first.", 2)
    return None


def _client(args):
    from .runner import _client as make
    return make(args)


def _new_output(path):
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise CliError("OUTPUT_PATH", "Output must be a new file in an existing directory.", 2)
    return path


def _input_file(path):
    if path is not None and not path.is_file():
        raise CliError("INPUT_FILE", "Every uploaded file must exist and be a regular file.", 2)
    return path


def _poll(client, args, read, done):
    """Call ``read`` until ``done(value)``; return (value, finished)."""
    deadline = time.monotonic() + args.max_wait
    while True:
        client.timeout = min(args.timeout, max(0.001, deadline - time.monotonic()))
        value = read()
        if done(value):
            return value, True
        if time.monotonic() >= deadline:
            return value, False
        time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))


def simple(name):
    def handler(args):
        return _call(_client(args), name, _params(args))
    return handler


def task_simple(name):
    def handler(args):
        params = _params(args, task_key=args.tid)
        gated = _gate(args, [(name, params, None, None)])
        if gated:
            return gated
        return _call(_client(args), name, params)
    return handler


def ut_job_simple(name):
    def handler(args):
        params = _params(args, job_id=args.job_id)
        from .catalog import get_operation
        if get_operation(name).mutating:
            gated = _gate(args, [(name, params, None, None)])
            if gated:
                return gated
        return _call(_client(args), name, params)
    return handler


# --------------------------------------------------------------------------- #
# Silver tasks
# --------------------------------------------------------------------------- #
def task_search(args):
    from .commands import resolve_project

    client = _client(args)
    project = resolve_project(client, args.project) if args.project is not None else None
    query = [(key, str(value)) for key, value in (("project_id", project), ("q", args.q),
                                                 ("status", args.status), ("limit", args.limit))
             if value is not None]
    if args.mine:
        query.append(("mine", "1"))
    return _call(client, "me.me_tasks", query=query)


def _passed(entry):
    """A run passes only with a PASS verdict; a missing or UNKNOWN verdict is not evidence."""
    return entry.get("status") == "passed" and str(entry.get("verdict") or "").upper() == "PASS"


def _model(task):
    name, version = task.get("sil_name") or "", task.get("sil_version") or ""
    return f"{name}@{version}" if version else name


def _outcomes(client, pid, keys):
    results = []
    for key in keys:
        params = {"project_id": pid, "task_key": key}
        task = _call(client, "tasks.project_task_detail", params)["data"]["task"]
        entry = {"task_id": key, "test_id": task.get("test_id"), "status": task.get("status"),
                 "result": task.get("result"), "model": _model(task),
                 "finished_at": task.get("finished_at"), "message": task.get("message", "")}
        if task.get("status") in TASK_DONE:
            verdict = _call(client, "tasks.project_task_jdgrslt", params)["data"]
            entry.update(verdict=verdict.get("verdict"), failed_steps=verdict.get("failed_steps"),
                         jdgrslt=verdict.get("content", ""))
            if "steps" in verdict:  # servers that structure jdgrslt per step
                entry["step_results"] = verdict["steps"]
                entry["failed_checks"] = [
                    {"step": step["step"], **check} for step in verdict["steps"]
                    for check in step.get("checks", []) if not check.get("ok")]
        entry["error_events"] = [event for event in task.get("events", []) if event.get("event_type") == "error"]
        results.append(entry)
    return results


def _await_tasks(client, args, keys):
    from .runner import _ok

    def read():
        return [_call(client, "tasks.project_task_status", {"project_id": args.pid, "task_key": key})["data"]["task"]
                for key in keys]

    states, finished = _poll(client, args, read, lambda tasks: all(t.get("status") in TASK_DONE for t in tasks))
    if not finished:
        raise CliError("WAIT_TIMEOUT", "Wait expired; the remote runs were not cancelled.", 9,
                       details={"tasks": [{"task_id": t.get("task_id"), "status": t.get("status")} for t in states]})
    outcomes = _outcomes(client, args.pid, keys)
    all_passed = all(_passed(entry) for entry in outcomes)
    return _ok({"tasks": outcomes, "all_passed": all_passed}), 0 if all_passed else 8


def task_run(args):
    selection = next((name for name in ("failed", "not_run", "all") if getattr(args, name)), None)
    if bool(args.test_ids) == bool(selection):
        raise CliError("ARGUMENT_ERROR", "Give --test-id, or one of --failed, --not-run or --all.", 2)
    body = {"model": args.model, "test_ids": args.test_ids or [f"<every {selection} case from case list>"]}
    gated = _gate(args, [("tasks.run_selected_tasks", _params(args), None, body)],
                  wait=args.wait, selection=selection)
    if gated:
        return gated
    client = _client(args)
    if selection:
        rows = _case_rows(client, args.pid, status="not_run" if selection == "not_run" else None,
                          failed=selection == "failed")["cases"]
        if not rows:
            from .runner import _ok
            return _ok({"created": [], "missing": [], "errors": [], "duplicates": [], "selection": selection,
                        "message": "No case matches this selection."})
        body["test_ids"] = [row["test_id"] for row in rows]
    response = _call(client, "tasks.run_selected_tasks", _params(args), body=body)
    data = response["data"]
    if not args.wait:
        return response
    keys = [entry["task_id"] for entry in data.get("created", [])]
    if not keys:
        return response, 8
    result, code = _await_tasks(client, args, keys)
    result["data"]["submission"] = data
    if data.get("missing") or data.get("errors") or data.get("duplicates"):
        code = 8
    return result, code


def task_wait(args):
    return _await_tasks(_client(args), args, args.tids)


def task_download(args):
    _new_output(args.output)
    return _call(_client(args), "tasks.download_project_task", _params(args, task_key=args.tid),
                 output=args.output)


def _items(client, pid, sheet):
    page, items = 1, []
    while True:
        data = _call(client, "items.list_items", {"project_id": pid},
                     query=[("sheet", sheet), ("page", str(page)), ("page_size", "500")])["data"]
        items.extend(data["items"])
        if not data["items"] or data["page"] * data["page_size"] >= data["total"]:
            return items
        page += 1


def _steps(item):
    document = item.get("steps")
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError:
            return {"raw": document}
    return document or {"input_signals": [], "expected_signals": [], "steps": []}


def _markdown(cases):
    lines = ["# Test report", ""]
    for case in cases:
        run = case.get("latest_run") or {}
        lines += [f"## {case['test_id']} {case.get('title') or ''}".rstrip(), "",
                  f"- Status: {run.get('status', 'not run')}",
                  f"- Verdict: {run.get('verdict') or run.get('result') or '-'}"]
        if run.get("task_id"):
            lines.append(f"- Task: {run['task_id']} ({run.get('model')}, finished {run.get('finished_at') or '-'})")
        steps = (case.get("steps") or {}).get("steps") if isinstance(case.get("steps"), dict) else None
        if steps:
            lines += ["", "| # | Step |", "|---|---|"]
            for number, step in enumerate(steps, 1):
                text = json.dumps(step, ensure_ascii=False).replace("|", "\\|")
                lines.append(f"| {number} | {text} |")
        if run.get("message"):
            lines.append(f"- Message: {run['message']}")
        if run.get("error_events"):
            lines += ["", "Errors:", "", "```", *[event.get("message", "") for event in run["error_events"]], "```"]
        if run.get("jdgrslt"):
            lines += ["", "jdgrslt:", "", "```", run["jdgrslt"].rstrip(), "```"]
        lines.append("")
    return "\n".join(lines)


def task_report(args):
    from .runner import _ok

    if args.output is not None:
        _new_output(args.output)
    client = _client(args)
    items = _items(client, args.pid, "test")
    listing = _call(client, "tasks.list_project_tasks", _params(args), query=[("limit", "2000")])["data"]
    latest = {}
    for task in listing["tasks"]:  # newest first
        latest.setdefault(task.get("test_id"), task)
    wanted = set(args.test_ids or [])
    cases = []
    for item in items:
        test_id = str(item.get("test_id") or "").strip() or str(item.get("case_id") or "").strip()
        if not test_id or (wanted and test_id not in wanted):
            continue
        task = latest.get(test_id)
        status = task.get("status") if task else "not_run"
        if args.status and status != args.status:
            continue
        case = {"test_id": test_id, "item_id": item["id"], "title": item.get("title") or item.get("test_name")}
        if not args.no_steps:
            case["steps"] = _steps(item)
        case["latest_run"] = _outcomes(client, args.pid, [task["task_id"]])[0] if task else None
        cases.append(case)
    data = {"cases": cases, "returned": len(cases), "missing": sorted(wanted - {case["test_id"] for case in cases}),
            "runs_scanned": len(listing["tasks"]), "runs_truncated": listing.get("truncated", False)}
    if args.output is not None:
        text = (_markdown(cases) if args.output.suffix.lower() == ".md"
                else json.dumps(data, ensure_ascii=False, indent=1))
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        data["output"] = str(args.output)
    return _ok(data)


CONTEXT_SHEETS = ("const", "io", "lib")


def _empty(value):
    return value is None or value == "" or value == [] or value == {}


def _value(item, field):
    """A field's value; steps-typed fields stored as JSON text are returned as objects."""
    value = item.get(field["field_key"])
    if field.get("data_type") == "steps" and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _labelled(item, fields, sheet, exclude=()):
    """The item's non-empty values for the sheet's fields, in field order, with display names."""
    return [{"field": field["field_key"], "name": field.get("display_name"), "value": _value(item, field)}
            for field in fields
            if (field.get("sheet") or "test") == sheet and field["field_key"] not in exclude
            and not _empty(item.get(field["field_key"]))]


def _context(client, pid, fields, sheets):
    """Every row of the project's shared sheets (const, io, lib) that each Silver run is generated from."""
    result = {}
    for sheet in sheets:
        keys = [field for field in fields if (field.get("sheet") or "test") == sheet]
        rows = []
        for item in _items(client, pid, sheet):
            values = {field["field_key"]: _value(item, field) for field in keys
                      if not _empty(item.get(field["field_key"]))}
            rows.append({"item_id": item["id"], **values})
        result[sheet] = {"fields": {field["field_key"]: field.get("display_name") for field in keys},
                         "rows": rows, "returned": len(rows)}
    return result


def case_show(args):
    from .commands import _test_id
    from .runner import _ok

    client = _client(args)
    fields = _call(client, "fields.list_fields", _params(args))["data"]["fields"]
    matches = [item for item in _items(client, args.pid, "test") if _test_id(item) == args.test_id]
    if not matches:
        raise CliError("NOT_FOUND", "No test-sheet case has this test id; see case list.", 2, status=404)
    if len(matches) > 1:
        raise CliError("AMBIGUOUS_TEST_ID", "Several test rows share this test id.", 2,
                       details=[{"item_id": item["id"]} for item in matches])
    item = matches[0]
    listing = _call(client, "tasks.list_project_tasks", _params(args), query=[("limit", "2000")])["data"]
    task = next((row for row in listing["tasks"] if row.get("test_id") == args.test_id), None)  # newest first
    data = {"test_id": args.test_id, "item_id": item["id"], "version": item.get("version"),
            "title": item.get("title") or item.get("test_name"),
            "info": _labelled(item, fields, "test", exclude={"steps"}),
            "steps": _steps(item),
            "latest_run": _outcomes(client, args.pid, [task["task_id"]])[0] if task else None,
            "runs_truncated": listing.get("truncated", False)}
    if args.context:
        data["context"] = _context(client, args.pid, fields, CONTEXT_SHEETS)
    return _ok(data)


def project_context(args):
    from .runner import _ok

    client = _client(args)
    fields = _call(client, "fields.list_fields", _params(args))["data"]["fields"]
    return _ok(_context(client, args.pid, fields, args.sheet or CONTEXT_SHEETS))


def _case_failed(run):
    return run is not None and (run.get("status") in {"failed", "cancelled"}
                                or str(run.get("result") or "").upper() in {"FAIL", "FAILED", "ERROR", "NG"})


def case_list(args):
    """One compact row per test case: the test id other commands take, plus its latest run."""
    from .runner import _ok

    data = _case_rows(_client(args), args.pid, status=args.status, failed=args.failed)
    return _ok({**data, "next": "Use the test_id with case show, steps show/edit/add/remove/move --test-id, or task run --test-id."})


def _case_rows(client, pid, *, status=None, failed=False):
    from .commands import _test_id

    items = _items(client, pid, "test")
    listing = _call(client, "tasks.list_project_tasks", {"project_id": pid}, query=[("limit", "2000")])["data"]
    latest = {}
    for task in listing["tasks"]:  # newest first
        latest.setdefault(task.get("test_id"), task)
    rows, counts = [], {}
    for item in items:
        test_id = _test_id(item)
        if not test_id:
            continue
        task = latest.get(test_id)
        state = task.get("status") if task else "not_run"
        if (status and state != status) or (failed and not _case_failed(task)):
            continue
        counts[state] = counts.get(state, 0) + 1
        rows.append({"test_id": test_id, "item_id": item["id"],
                     "title": item.get("title") or item.get("test_name"),
                     "status": state, "result": task.get("result") if task else None,
                     "task_id": task.get("task_id") if task else None,
                     "finished_at": task.get("finished_at") if task else None})
    return {"cases": rows, "returned": len(rows), "statuses": counts,
            "runs_scanned": len(listing["tasks"]), "runs_truncated": listing.get("truncated", False)}


# Execution results and row identity are never copied onto a new case.
_NOT_COPIED = {"id", "uuid", "version", "row_order", "updated_at", "updated_by", "case_id", "test_id",
               "result", "actual_result", "exec_date", "executor", "log", "workflow_status"}
_ROW_KEYS = {"const": "const_name", "io": "io_name", "lib": "lib_func"}


def _patch(client, pid, item, changes):
    version = item.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CliError("INVALID_RESPONSE", "Current item has no valid version; refusing to update.", 7)
    return _call(client, "items.patch_item", {"project_id": pid, "item_id": item["id"]},
                 body={"version": version, "changes": changes})


def _changes(args, forbidden=()):
    from .commands import _assignments

    changes = _assignments(args.assignments)
    blocked = sorted(set(changes) & set(forbidden))
    if blocked:
        raise CliError("FIELD_NOT_ALLOWED", "Use the steps commands to change steps: " + ", ".join(blocked), 2)
    return changes


def _write_plan(args, name, params, body, **details):
    """Dry-run plan for "find the row, then write it"; the row's ID is only known online."""
    gated = _gate(args, [("items.list_items", _params(args), None, None), (name, params, None, body)], **details)
    if gated:
        write = gated["data"]["requests"][1]
        write["path"] = write["path"].rsplit("/", 1)[0] + "/<item found by the list read>"
    return gated


def case_set(args):
    from .commands import _item

    changes = _changes(args, forbidden={"steps"})
    params = _params(args, item_id=1)
    gated = _write_plan(args, "items.patch_item", params, {"version": "<current>", "changes": changes},
                        test_id=args.test_id)
    if gated:
        return gated
    client = _client(args)
    _, item = _item(client, _params(args), test_id=args.test_id)
    return _patch(client, args.pid, item, changes)


def case_add(args):
    from .commands import _item
    from .runner import _json_input

    values = _json_input(args.json_input)
    if not str(values.get("test_id") or "").strip():
        raise CliError("INVALID_CASE", "The case needs a test_id.", 2)
    body = {"sheet": "test", "values": values}
    gated = _gate(args, [("items.create_item", _params(args), None, body)], after=args.after)
    if gated:
        return gated
    client = _client(args)
    existing = [row for row in _case_rows(client, args.pid)["cases"] if row["test_id"] == values["test_id"].strip()]
    if existing:
        raise CliError("DUPLICATE", "A case with this test_id already exists.", 2, details=existing)
    if args.after:
        _, anchor = _item(client, _params(args), test_id=args.after)
        body.update(anchor_id=anchor["id"], place="below")
    return _call(client, "items.create_item", _params(args), body=body)


def case_copy(args):
    from .commands import _item, _test_id

    body = {"sheet": "test", "values": "<source fields without results>", "anchor_id": "<source>", "place": "below"}
    gated = _gate(args, [("items.create_item", _params(args), None, body)], source=args.test_id, new_id=args.new_id)
    if gated:
        return gated
    client = _client(args)
    items = _items(client, args.pid, "test")
    if any(_test_id(item) == args.new_id for item in items):
        raise CliError("DUPLICATE", "A case with the new test id already exists.", 2)
    _, source = _item(client, _params(args), test_id=args.test_id)
    values = {key: copy.deepcopy(value) for key, value in source.items() if key not in _NOT_COPIED}
    values.update(test_id=args.new_id, case_id=args.new_id)
    body = {"sheet": "test", "values": values, "anchor_id": source["id"], "place": "below"}
    return _call(client, "items.create_item", _params(args), body=body)


def case_delete(args):
    from .commands import _item

    gated = _write_plan(args, "items.delete_item", _params(args, item_id=1), None, test_id=args.test_id,
                        note="Soft delete: the case goes to the recycle bin and can be restored.")
    if gated:
        return gated
    client = _client(args)
    _, item = _item(client, _params(args), test_id=args.test_id)
    response = _call(client, "items.delete_item", _params(args, item_id=item["id"]))
    response["data"] = {**(response.get("data") or {}), "item_id": item["id"], "test_id": args.test_id}
    return response


def row_set(args):
    changes = _changes(args)
    key_field = _ROW_KEYS[args.sheet]
    gated = _write_plan(args, "items.patch_item", _params(args, item_id=1), {"version": "<current>", "changes": changes},
                        sheet=args.sheet, key={key_field: args.key})
    if gated:
        return gated
    client = _client(args)
    matches = [item for item in _items(client, args.pid, args.sheet)
               if str(item.get(key_field) or "").strip() == args.key]
    if len(matches) != 1:
        raise CliError("NOT_FOUND" if not matches else "AMBIGUOUS_KEY",
                       f"No {args.sheet} row has this {key_field}." if not matches
                       else f"Several {args.sheet} rows share this {key_field}.", 2,
                       details=[{"item_id": item["id"]} for item in matches])
    return _patch(client, args.pid, matches[0], changes)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def model_upload(args):
    files = [("dll", _input_file(args.dll)), ("sbs", _input_file(args.sbs))]
    if args.pdb is not None:
        files.append(("pdb", _input_file(args.pdb)))
    files += [("extra", _input_file(path)) for path in args.extra]
    form = [("name", args.name), ("version", args.version), ("version_note", args.note)]
    gated = _gate(args, [("models.upload_project_model", _params(args), None, None)],
                  form_fields=[key for key, _ in form], file_fields=[key for key, _ in files])
    if gated:
        return gated
    return _call(_client(args), "models.upload_project_model", _params(args), form=form, files=files)


def model_use(args):
    body = {"name": args.name}
    if args.model_id is not None:
        body["model_id"] = args.model_id
    gated = _gate(args, [("models.set_current_project_model", _params(args), None, body)])
    if gated:
        return gated
    return _call(_client(args), "models.set_current_project_model", _params(args), body=body)


# --------------------------------------------------------------------------- #
# Unit-test pipeline
# --------------------------------------------------------------------------- #
def ut_configure(args):
    from .runner import _json_input

    body = _json_input(args.json_input)
    gated = _gate(args, [("ut_pipeline.put_config", _params(args), None, body)])
    if gated:
        return gated
    return _call(_client(args), "ut_pipeline.put_config", _params(args), body=body)


def ut_validate(args):
    """Validation persists nothing, so it needs no --confirm."""
    from .runner import _json_input

    body = {"manifest_document": _json_input(args.json_input)}
    return _call(_client(args), "ut_pipeline.validate_manifest", _params(args), body=body)


def ut_paths(args):
    return _call(_client(args), "ut_pipeline.browse_server_paths", _params(args),
                 query=[("path", args.path), ("kind", args.kind)])


def ut_import(args):
    gated = _gate(args, [("ut_pipeline.import_targets", _params(args), None, {})])
    if gated:
        return gated
    return _call(_client(args), "ut_pipeline.import_targets", _params(args), body={})


def _target_rows(client, pid):
    return _call(client, "ut_pipeline.list_targets", {"project_id": pid})["data"]


def ut_targets(args):
    from .runner import _ok

    rows = _target_rows(_client(args), args.pid)
    if not args.all:
        rows = [row for row in rows if row.get("in_index")]
    if args.function:
        rows = [row for row in rows if row.get("function") in set(args.function)]
    if args.source:
        rows = [row for row in rows if args.source in (row.get("source_rel") or "")]
    return _ok({"targets": rows, "returned": len(rows)})


def _resolve_targets(client, args):
    ids = list(args.target_id)
    if not args.function:
        return ids
    rows = [row for row in _target_rows(client, args.pid) if row.get("in_index")]
    if args.source:
        rows = [row for row in rows if args.source in (row.get("source_rel") or "")]
    problems = {}
    for name in args.function:
        matches = [row for row in rows if row.get("function") == name]
        if len(matches) == 1:
            ids.append(matches[0]["id"])
        else:
            problems[name] = [{"id": row["id"], "source_rel": row.get("source_rel")} for row in matches]
    if problems:
        raise CliError("TARGET_UNRESOLVED", "Each function must match exactly one indexed target; "
                       "use --source or --target-id (empty list: not found).", 2, details=problems)
    return ids


def _job_summary(job):
    return {key: job.get(key) for key in ("id", "batch_id", "state", "last_verdict", "gate_code", "reason",
                                          "repair_rounds", "current_attempt_id", "coverage")} | {
        "function": (job.get("target") or {}).get("function")}


def _await_jobs(client, args, batch=None, job_ids=()):
    from .runner import _ok

    def read():
        if batch is not None:
            return _call(client, "ut_pipeline.list_jobs", {"project_id": args.pid},
                         query=[("batch_id", str(batch))])["data"]
        return [_call(client, "ut_pipeline.job_detail", {"project_id": args.pid, "job_id": job})["data"]
                for job in job_ids]

    jobs, finished = _poll(client, args, read, lambda rows: all(row.get("state") not in UT_ACTIVE for row in rows))
    summary = [_job_summary(job) for job in jobs]
    if not finished:
        raise CliError("WAIT_TIMEOUT", "Wait expired; the remote jobs were not stopped.", 9, details={"jobs": summary})
    counts = {}
    for job in summary:
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    return _ok({"jobs": summary, "states": counts,
                "next": "Inspect with ut evidence; awaiting_review needs a human review decision."})


def ut_run(args):
    from .commands import _request_spec

    if not args.function and not args.target_id:
        raise CliError("ARGUMENT_ERROR", "Give --function or --target-id.", 2)
    if args.dry_run:
        body = {"target_ids": args.target_id or ["<resolved from --function>"], "label": args.label}
        return _gate(args, [("ut_pipeline.create_batch", _params(args), None, body)],
                     functions=args.function, source=args.source, wait=args.wait,
                     reads=[_request_spec("ut_pipeline.list_targets", _params(args))] if args.function else [])
    _gate(args, [])
    client = _client(args)
    ids = _resolve_targets(client, args)
    response = _call(client, "ut_pipeline.create_batch", _params(args), body={"target_ids": ids, "label": args.label})
    if not args.wait:
        return response
    result = _await_jobs(client, args, batch=response["data"]["id"])
    result["data"]["batch"] = response["data"]
    return result


def ut_wait(args):
    if (args.batch is None) == (not args.job):
        raise CliError("ARGUMENT_ERROR", "Give either --batch or --job.", 2)
    return _await_jobs(_client(args), args, batch=args.batch, job_ids=args.job)


def ut_jobs(args):
    query = [(key, str(value)) for key, value in (("state", args.state), ("batch_id", args.batch)) if value is not None]
    return _call(_client(args), "ut_pipeline.list_jobs", _params(args), query=query)


def _section(read):
    try:
        return read()["data"]
    except CliError as exc:
        return {"unavailable": True, "code": exc.code, "message": exc.message}


def ut_evidence(args):
    from .runner import _ok

    client = _client(args)
    job = _call(client, "ut_pipeline.job_detail", _params(args, job_id=args.job_id))["data"]
    attempt_id = args.attempt or job.get("current_attempt_id")
    data = {"job": _job_summary(job), "attempt_id": attempt_id}
    if attempt_id is None:
        data["message"] = "The job has no attempt yet."
        return _ok(data)
    attempt = next((row for row in job.get("attempts", []) if row.get("id") == attempt_id), None)
    if attempt is None:
        raise CliError("NOT_FOUND", "Attempt does not belong to this job.", 2, status=404)
    params = _params(args, job_id=args.job_id, attempt_id=attempt_id)
    data["attempt"] = attempt
    data["csv"] = _section(lambda: _call(client, "ut_pipeline.attempt_csv", params, query=[("format", "text")]))
    data["diagnostic"] = _section(lambda: _call(client, "ut_pipeline.attempt_csv", params,
                                                query=[("format", "diagnostic")]))
    data["coverage"] = _section(lambda: _call(client, "ut_pipeline.attempt_coverage", params))
    return _ok(data)


def ut_revise(args):
    _input_file(args.csv)
    params = _params(args, job_id=args.job_id)
    gated = _gate(args, [("ut_pipeline.submit_revision", params, None, None)],
                  form_fields=["note"], file_fields=["file"])
    if gated:
        return gated
    return _call(_client(args), "ut_pipeline.submit_revision", params,
                 form=[("note", args.note)], files=[("file", args.csv)])


def ut_stop_batch(args):
    params = _params(args, batch_id=args.batch)
    gated = _gate(args, [("ut_pipeline.stop_batch", params, None, {})])
    if gated:
        return gated
    return _call(_client(args), "ut_pipeline.stop_batch", params, body={})


def ut_review(args):
    params = _params(args, job_id=args.job_id)
    body = {"decision": args.decision, "note": args.note, "expected_attempt_id": "<current attempt id>"}
    gated = _gate(args, [("ut_pipeline.job_detail", params, None, None), ("ut_pipeline.review", params, None, body)],
                  reads="The current attempt id is read first and sent as expected_attempt_id, "
                        "so a candidate created after that read is never signed off.")
    if gated:
        return gated
    client = _client(args)
    job = _call(client, "ut_pipeline.job_detail", params)["data"]
    body["expected_attempt_id"] = job.get("current_attempt_id")
    return _call(client, "ut_pipeline.review", params, body=body)


def ut_export(args):
    _new_output(args.output)
    query = [("batch_id", str(args.batch))] if args.batch is not None else []
    return _call(_client(args), "ut_pipeline.export_approved", _params(args), query=query, output=args.output)
