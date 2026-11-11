"""Task-oriented commands built on the discovered HTTP operations."""

from __future__ import annotations

import argparse
import copy
import json
import os

from . import workflows
from .catalog import get_operation
from .transport import CliError


def _positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected an integer greater than zero.")
    return number


def register(commands):
    from .runner import _positive

    groups = {
        "project": ("List, inspect or create projects.", "project show 1"),
        "item": ("List or inspect matrix items, including their current version.", "item list 1 --sheet test"),
        "steps": ("Read or update an item's steps document with optimistic version locking.", "steps show 1 2"),
        "task": ("Search, run, wait for, report on, inspect or rerun Silver test runs.", "task failures 1"),
    }
    actions = {}

    def group(name, description, example):
        parser = commands.add_parser(
            name, help=description, description=description,
            epilog=f"Example: python3 -m silver_cli {example}",
        )
        actions[name] = parser.add_subparsers(dest="action", required=True)

    for name, (description, example) in groups.items():
        group(name, description, example)

    def command(group, name, description, example, *, project=True, item=False,
                task=False, job=False, write=False, handler=None):
        parser = actions[group].add_parser(
            name, help=description, description=description,
            epilog=f"Example: python3 -m silver_cli {group} {example}",
        )
        parser.set_defaults(high_level=True, handler=handler)
        if project:
            parser.add_argument("pid", type=_positive_int, help="Numeric project ID.")
        if item:
            parser.add_argument("iid", type=_positive_int, help="Numeric matrix item ID (not case_id).")
        if task:
            parser.add_argument("tid", help="Task key returned as task_id by the API (not the database ID).")
        if job:
            parser.add_argument("job_id", type=_positive_int, help="Numeric unit-test job ID.")
        parser.add_argument("--url", default=os.environ.get("SILVER_CLI_URL"),
                            help="Server base URL; defaults to SILVER_CLI_URL.")
        parser.add_argument("--timeout", type=_positive, default=30.0,
                            help="Per-request timeout in seconds (default: 30).")
        parser.add_argument("--allow-http", action="store_true",
                            help="Allow plain HTTP for a trusted server; HTTPS is the default requirement.")
        if write:
            guard = parser.add_mutually_exclusive_group()
            guard.add_argument("--dry-run", action="store_true",
                               help="Print an offline request plan; do not log in or send any requests.")
            guard.add_argument("--confirm", action="store_true",
                               help="Authorize this write, including any required preceding reads.")
        return parser

    projects = command("project", "list", "List accessible projects and their card statistics.",
                       "list --search brake", project=False)
    projects.add_argument("--search", help="Keep projects whose code, name or description contains this text (case-insensitive).")
    command("project", "show", "Combine project details, fields, project models and dashboard statistics.", "show 1")
    create = command("project", "create", "Create a project; requires --confirm to send the POST.",
                     "create --json @project.json --dry-run", project=False, write=True)
    create.add_argument("--json", dest="json_input", required=True, metavar="JSON|@FILE|@-",
                        help="Project object: code, name, optional description and project_type (silver or unit_test; ut is accepted).")
    listing = command("item", "list", "List one page of matrix items; server pagination defaults apply.",
                      "list 1 --sheet test --page 1 --page-size 100")
    listing.add_argument("--sheet", help="Sheet name, for example test, lib, const or io; default: all sheets.")
    listing.add_argument("--page", type=_positive_int, help="Page number, starting at 1 (server default: 1).")
    listing.add_argument("--page-size", type=_positive_int,
                         help="Items per page; the server applies its configured maximum.")
    command("item", "show", "Find an item by numeric ID by scanning item-list pages; no single-item GET exists.",
            "show 1 2", item=True)
    command("steps", "show", "Return the item's steps document: input_signals, expected_signals and steps.",
            "show 1 2", item=True)
    setting = command("steps", "set", "Replace the steps document after reading the current version. VERSION_CONFLICT is never retried.",
                      "set 1 2 --json @steps.json --confirm", item=True, write=True)
    setting.add_argument("--json", dest="json_input", required=True, metavar="JSON|@FILE|@-",
                         help="Steps document object containing input_signals, expected_signals and steps arrays.")
    editing = command("steps", "edit", "Edit fields of one step, preserving other steps and signal definitions. VERSION_CONFLICT is never retried.",
                      "edit 1 2 --step 1 --set purpose=check timing=10 --confirm", item=True, write=True)
    editing.add_argument("--step", type=_positive_int, required=True,
                         help="One-based position in the steps array (1 is the first step).")
    editing.add_argument("--set", dest="assignments", action="append", nargs="+", required=True,
                         metavar="KEY=VALUE", help="Step fields to replace; repeat --set or supply several pairs. Values parse as JSON when valid, otherwise as text. Keys are literal, not dotted paths.")
    tasks = command("task", "list", "List the newest 2000 runs; report truncation. Status filtering happens locally because the API has no status filter.",
                    "list 1 --status failed")
    tasks.add_argument("--status", choices=("queued", "running", "passed", "failed", "cancelled"),
                       help="Keep only runs with this execution status (distinct from their judge verdict).")
    command("task", "show", "Read task detail, including review information and up to the first 5000 events.",
            "show 1 TASK_KEY", task=True)
    failures = command("task", "failures", "List only failed/error cases by execution status or judge verdict, with jdgrslt and error events. Project scans cover the newest 2000 runs and report truncation.",
                       "failures 1 --task TASK_KEY")
    failures.add_argument("--task", dest="tid", help="Inspect only this task key instead of listing project runs.")
    log = command("task", "log", "Read log/warning/error events and task errors from detail. The API returns only the first 5000 events, so tail applies to that available window.",
                  "log 1 TASK_KEY --tail 50", task=True)
    log.add_argument("--tail", type=_positive_int, help="Keep the last N lines of the available log; errors remain included separately.")
    command("task", "rerun", "Rerun one saved task via rerun-selected using its original model and current matrix inputs. Existing results are overwritten; queued/running tasks are skipped. Inspect created/skipped/missing/errors in the response.",
            "rerun 1 TASK_KEY --confirm", task=True, write=True)
    workflows.register(command, group)


def _request_spec(name, params=None, *, query=None, body=None):
    from .runner import _path

    operation = get_operation(name)
    path = _path(operation, [f"{key}={value}" for key, value in (params or {}).items()])
    return {"operation": name, "method": operation.method, "path": path,
            "query": query or [], "json_body": body}


def _request(client, name, params=None, *, query=None, body=None):
    spec = _request_spec(name, params, query=query, body=body)
    return client.request(spec["method"], spec["path"], query=spec["query"], json_body=body)


def _item(client, params, item_id):
    page = 1
    while True:
        response = _request(client, "items.list_items", params,
                            query=[("page", str(page)), ("page_size", "500")])
        data = response["data"]
        for item in data["items"]:
            if item["id"] == item_id:
                return response, item
        if not data["items"] or data["page"] * data["page_size"] >= data["total"]:
            raise CliError("NOT_FOUND", "Item ID was not found in this project's live items.", 2, status=404)
        page += 1


def _document(document):
    if not isinstance(document, dict) or any(
        not isinstance(document.get(key), list)
        for key in ("input_signals", "expected_signals", "steps")
    ) or any(not isinstance(step, dict) for step in document["steps"]):
        raise CliError("INVALID_STEPS", "Steps must be an object with input_signals, expected_signals and steps arrays; each step must be an object.", 2)
    return document


def _assignments(groups):
    from .runner import _invalid_constant, _pairs, _unique_object

    changes = {}
    for key, value in _pairs([pair for group in groups for pair in group]):
        if key in changes:
            raise CliError("INVALID_PAIR", "Each step field may be assigned only once.", 2)
        try:
            changes[key] = json.loads(value, object_pairs_hook=_unique_object,
                                      parse_constant=_invalid_constant)
        except json.JSONDecodeError:
            changes[key] = value
    return changes


def _failed(task):
    return (str(task.get("status", "")).lower() in {"failed", "error"}
            or str(task.get("result", "")).upper() in {"FAIL", "FAILED", "ERROR"})


def _errors(task):
    return {"message": task.get("message", ""),
            "events": [event for event in task.get("events", []) if event.get("event_type") == "error"]}


def execute(args, secrets):
    from .runner import _client, _collect_secrets, _json_input, _ok

    if args.handler is not None:
        return args.handler(args)
    group, action = args.command, args.action
    params = {"project_id": args.pid} if hasattr(args, "pid") else {}
    write = (group, action) in {("project", "create"), ("steps", "set"),
                                ("steps", "edit"), ("task", "rerun")}
    body = _json_input(args.json_input) if hasattr(args, "json_input") else None
    edits = _assignments(args.assignments) if group == "steps" and action == "edit" else None
    secrets.update(_collect_secrets(body))
    secrets.update(_collect_secrets(edits))
    if group == "steps" and action == "set":
        _document(body)
    if group == "task" and getattr(args, "tid", None):
        _request_spec("tasks.project_task_detail", {**params, "task_key": args.tid})
    if write and args.dry_run:
        if group == "project":
            plan = [_request_spec("projects.create_project", body=body)]
        elif group == "task":
            plan = [_request_spec("tasks.rerun_selected_tasks", params, body={"task_keys": [args.tid]})]
        else:
            plan = [_request_spec("items.list_items", params, query=[("page", "1"), ("page_size", "500")]),
                    _request_spec("items.patch_item", {**params, "item_id": args.iid},
                                  body={"version": "<current item version>",
                                        "changes": {"steps": body if action == "set" else "<current document with requested edits>"}})]
        result = {"dry_run": True, "requests": plan}
        if group == "steps":
            result.update({"item_id": args.iid, "reads": "Scan item-list pages until the ID is found; use its version in PATCH."})
            if edits is not None:
                result.update({"step": args.step, "set": edits})
        return _ok(result)
    if write and not args.confirm:
        raise CliError("CONFIRM_REQUIRED", "Mutating commands require --confirm; inspect --dry-run first.", 2)
    client = _client(args)
    if group == "project":
        if action == "list":
            response = _request(client, "projects.list_projects")
            if args.search:
                needle = args.search.casefold()
                data = dict(response["data"])
                data["projects"] = [project for project in data["projects"]
                                    if any(needle in str(project.get(key) or "").casefold()
                                           for key in ("code", "name", "description"))]
                return {**response, "data": data}
            return response
        if action == "create":
            return _request(client, "projects.create_project", body=body)
        response = _request(client, "projects.get_project", params)
        data = dict(response["data"])
        data["fields"] = _request(client, "fields.list_fields", params)["data"]["fields"]
        data["models"] = _request(client, "models.list_project_models", params)["data"]["models"]
        data["dashboard"] = _request(client, "dashboard.project_dashboard_data", params)["data"]
        return {**response, "data": data}
    if group in {"item", "steps"}:
        if action == "list":
            query = [(key, str(getattr(args, key))) for key in ("sheet", "page", "page_size")
                     if getattr(args, key) is not None]
            return _request(client, "items.list_items", params, query=query)
        response, item = _item(client, params, args.iid)
        if group == "item":
            return {**response, "data": {"item": item}}
        document = None
        if action != "set":
            document = item.get("steps")
            if isinstance(document, str):
                document = json.loads(document)
            if document is None:
                document = {"input_signals": [], "expected_signals": [], "steps": []}
            document = _document(document)
        if action == "show":
            return {**response, "data": document}
        if action == "edit":
            document = copy.deepcopy(document)
            if args.step > len(document["steps"]):
                raise CliError("STEP_NOT_FOUND", "--step exceeds the number of steps in the document.", 2)
            document["steps"][args.step - 1].update(edits)
            body = document
        version = item.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise CliError("INVALID_RESPONSE", "Current item has no valid version; refusing to update.", 7)
        return _request(client, "items.patch_item", {**params, "item_id": args.iid},
                        body={"version": version, "changes": {"steps": body}})
    if action == "rerun":
        return _request(client, "tasks.rerun_selected_tasks", params, body={"task_keys": [args.tid]})
    if action in {"show", "log"}:
        response = _request(client, "tasks.project_task_detail", {**params, "task_key": args.tid})
        if action == "show":
            return response
        task = response["data"]["task"]
        events = task.get("events", [])
        lines = [line for event in events if event.get("event_type") in {"log", "warning", "error"}
                 for line in event.get("message", "").splitlines()]
        if args.tail is not None:
            lines = lines[-args.tail:]
        return {**response, "data": {"task_id": args.tid, "status": task.get("status"),
                "log": "\n".join(lines), "errors": _errors(task),
                "events_returned": len(events), "possibly_truncated": len(events) >= 5000,
                "event_window": "First 5000 events at most; tail applies within this window."}}
    if action == "failures" and args.tid:
        response = _request(client, "tasks.project_task_detail", {**params, "task_key": args.tid})
        tasks = [response["data"]["task"]]
        data = {"total": 1, "truncated": False}
    else:
        response = _request(client, "tasks.list_project_tasks", params, query=[("limit", "2000")])
        data = dict(response["data"])
        tasks = data["tasks"]
    if action == "list":
        if args.status:
            data["tasks"] = [task for task in tasks if task.get("status") == args.status]
        data["returned"] = len(data["tasks"])
        return {**response, "data": data}
    failures = []
    for task in tasks:
        if not _failed(task):
            continue
        task_params = {**params, "task_key": task["task_id"]}
        detail = task if args.tid else _request(client, "tasks.project_task_detail", task_params)["data"]["task"]
        verdict = _request(client, "tasks.project_task_jdgrslt", task_params)["data"]
        if not (_failed(detail) or _failed({"result": verdict.get("verdict")})):
            continue
        failures.append({**detail, "jdgrslt": verdict, "errors": _errors(detail)})
    return {**response, "data": {"failures": failures, "returned": len(failures),
            "tasks_scanned": len(tasks), "total": data["total"],
            "truncated": data.get("truncated", False)}}
