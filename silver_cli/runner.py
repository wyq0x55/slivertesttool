"""Non-interactive, JSON-first command dispatch over the existing HTTP API."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .catalog import get_operation, operations
from .commands import execute, register
from .transport import CliError, Client


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("ARGUMENT_ERROR", "Invalid arguments; use --help.", 2)


def _positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Expected a positive finite number.")
    return number


def _parser():
    parser = Parser(prog="silver_cli", description="Silver platform CLI. JSON stdout; no interactive prompts. "
                    "Set SILVER_CLI_USERNAME and SILVER_CLI_PASSWORD together for session login.",
                    epilog="Task commands: project list/show/create; item list/show; steps show/set/edit; "
                    "case list/show/set/add/copy/delete; row set; task search/list/show/run/wait/report/failures/log/rerun/cancel/download; "
                    "model list/upload/use; ut options/config/configure/validate-manifest/paths/import-targets/"
                    "targets/run/wait/jobs/batches/job/evidence/revise/retry/stop/stop-batch/review/export/stats. "
                    "Examples: python3 -m silver_cli project list; python3 -m silver_cli steps show 1 2; "
                    "python3 -m silver_cli describe items.patch_item")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Discover all operations offline",
                                  description="List HTTP operations derived from route source, without connecting to a server.",
                                  epilog="Example: python3 -m silver_cli list --group items")
    listing.add_argument("--group", help="Restrict operations to a module, for example items or tasks.")
    describe = commands.add_parser("describe", help="Inspect request field hints offline",
                                   description="Inspect one operation's endpoint, method and request hints offline.",
                                   epilog="Example: python3 -m silver_cli describe items.patch_item")
    describe.add_argument("operation", help="Operation name returned by list.")
    for command in ("call", "wait"):
        description = ("Call a discovered HTTP operation; writes require --confirm." if command == "call"
                       else "Poll a read-only JSON operation until it reaches a declared terminal state.")
        example = ("call projects.list_projects" if command == "call" else
                   "wait tasks.project_task_status --param project_id=1 --param task_key=TASK_KEY "
                   "--status-path data.task.status --success passed --pending queued --pending running --failure failed --failure cancelled")
        sub = commands.add_parser(command, help=description, description=description,
                                  epilog=f"Example: python3 -m silver_cli {example}")
        sub.add_argument("operation", help="Operation name returned by list.")
        sub.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="Path parameter; repeat for each declared parameter.")
        sub.add_argument("--query", action="append", default=[], metavar="NAME=VALUE", help="Query parameter; repeat as needed.")
        sub.add_argument("--url", default=os.environ.get("SILVER_CLI_URL"), help="Server base URL; defaults to SILVER_CLI_URL.")
        sub.add_argument("--timeout", type=_positive, default=30.0, help="Per-request timeout in seconds (default: 30).")
        sub.add_argument("--allow-http", action="store_true", help="Allow plain HTTP to a trusted server.")
        sub.add_argument("--dry-run", action="store_true", help="Print an offline request preview without sending requests.")
        if command == "call":
            sub.add_argument("--json", dest="json_input", metavar="JSON|@FILE|@-", help="JSON object, UTF-8 file or standard input.")
            sub.add_argument("--form", action="append", default=[], metavar="NAME=VALUE", help="Form field; repeat as needed.")
            sub.add_argument("--file", action="append", default=[], metavar="FIELD=PATH", help="Upload a local file under this form field.")
            sub.add_argument("--output", type=Path, help="New local path required for download operations; never overwritten.")
            sub.add_argument("--confirm", action="store_true", help="Authorize a mutating request.")
            sub.add_argument("--max-events", type=int, default=1000, help="Maximum stream events before stopping (default: 1000).")
        else:
            sub.add_argument("--status-path", required=True, help="Dotted response path containing the status, for example data.task.status.")
            sub.add_argument("--success", action="append", required=True, help="Successful terminal state; repeat as needed.")
            sub.add_argument("--pending", action="append", required=True, help="State that permits continued polling; repeat as needed.")
            sub.add_argument("--failure", action="append", default=[], help="Failed terminal state (exit 8); repeat as needed.")
            sub.add_argument("--interval", type=_positive, default=1.0, help="Polling interval in seconds (default: 1).")
            sub.add_argument("--max-wait", type=_positive, default=300.0, help="Maximum polling duration in seconds (default: 300).")
    register(commands)
    return parser


def _pairs(values):
    pairs = []
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\[\]-]*", key):
            raise CliError("INVALID_PAIR", "Expected NAME=VALUE.", 2)
        pairs.append((key, item))
    return pairs


def _path(operation, values):
    pairs = _pairs(values)
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) != set(operation.path_params):
        raise CliError("PATH_PARAMS", "Supply each declared path parameter exactly once.", 2)
    path = operation.path
    for name, converter in operation.path_params.items():
        value = params[name]
        if converter == "int":
            if not re.fullmatch(r"[0-9]+", value):
                raise CliError("PATH_PARAMS", "Integer path parameter required.", 2)
        elif not value or any(char in value for char in "/\\%?#") or value in {".", ".."}:
            raise CliError("PATH_PARAMS", "Path parameters must be single nonempty segments.", 2)
        if any(ord(char) < 32 for char in value):
            raise CliError("PATH_PARAMS", "Control characters are not allowed.", 2)
        path = re.sub(r"<(?:[^:<>]+:)?" + re.escape(name) + r">", lambda match: quote(value, safe=""), path)
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON numbers are not supported.")


def _json_input(value):
    if value is None:
        return None
    try:
        if value == "@-":
            value = sys.stdin.read()
        elif value.startswith("@"):
            value = Path(value[1:]).read_text(encoding="utf-8-sig")
        payload = json.loads(value.lstrip("\ufeff"), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        json.dumps(payload, allow_nan=False)
        return payload
    except (OSError, ValueError, RecursionError) as exc:
        raise CliError("INVALID_JSON", "Supply a readable UTF-8 JSON object without duplicate keys or non-finite numbers.", 2) from exc


def _sensitive(key):
    normalized = key.lower().replace("-", "_")
    return (normalized == "token" or normalized.endswith("_token")
            or any(word in normalized for word in ("password", "secret", "api_key", "authorization", "cookie")))


def _redact(value, secrets):
    if isinstance(value, dict):
        return {key: "[redacted]" if _sensitive(key) else _redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def _collect_secrets(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _sensitive(key) and isinstance(item, str) and item:
                found.add(item)
            found.update(_collect_secrets(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect_secrets(item))
    return found


def _emit(value, secrets):
    secrets.update(_collect_secrets(value))
    print(json.dumps(_redact(value, secrets), ensure_ascii=True, allow_nan=False), flush=True)


def _ok(data):
    return {"success": True, "data": data, "error": None, "request_id": None}


def _prepare(args, operation):
    path = _path(operation, args.param)
    query = _pairs(args.query)
    if args.command == "wait":
        if operation.method != "GET" or operation.response_kind != "json":
            raise CliError("WAIT_READ_ONLY", "Wait requires a read-only JSON operation.", 2)
        groups = [set(args.success), set(args.pending), set(args.failure)]
        if any(not status for group in groups for status in group) or any(groups[first] & groups[second] for first, second in ((0, 1), (0, 2), (1, 2))):
            raise CliError("WAIT_STATES", "Success, pending and failure states must be disjoint.", 2)
        if not all(args.status_path.split(".")):
            raise CliError("WAIT_PATH", "Use a dotted JSON object path.", 2)
        return path, {"query": query}
    form = _pairs(args.form)
    files = [(key, Path(value)) for key, value in _pairs(args.file)]
    payload = _json_input(args.json_input)
    if payload is not None and (form or files):
        raise CliError("BODY_CONFLICT", "JSON and multipart/form bodies cannot be combined.", 2)
    if operation.method == "GET" and (payload is not None or form or files):
        raise CliError("READ_BODY", "GET operations do not accept a request body.", 2)
    for key, path_value in files:
        if not path_value.is_file():
            raise CliError("INPUT_FILE", "Every uploaded file must exist and be a regular file.", 2)
    if args.max_events <= 0:
        raise CliError("STREAM_LIMIT", "Event limit must be positive.", 2)
    if operation.response_kind == "download" and args.output is None:
        raise CliError("OUTPUT_REQUIRED", "Downloads require an explicit --output path.", 2)
    if args.output is not None:
        if operation.response_kind != "download":
            raise CliError("OUTPUT_UNSUPPORTED", "Only download operations accept --output.", 2)
        if args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
            raise CliError("OUTPUT_PATH", "Output must be a new file in an existing directory.", 2)
    return path, {"query": query, "json_body": payload, "form": form, "files": files, "output": args.output}


def _wait(client, operation, path, kwargs, args, secrets):
    deadline = time.monotonic() + args.max_wait
    last = None
    while time.monotonic() < deadline:
        client.timeout = min(args.timeout, max(0.001, deadline - time.monotonic()))
        last = client.request(operation.method, path, **kwargs)
        status = last
        for key in args.status_path.split("."):
            if not isinstance(status, dict) or key not in status:
                raise CliError("WAIT_STATUS_MISSING", "Status path not present in response.", 7)
            status = status[key]
        if status in args.success or status in args.failure:
            _emit(last, secrets)
            return 0 if status in args.success else 8
        if status not in args.pending:
            raise CliError("WAIT_STATUS_UNKNOWN", "Unrecognized job state; inspect the object before retrying.", 7)
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    raise CliError("WAIT_TIMEOUT", "Wait expired; the remote job was not cancelled.", 9, details=last)


def _client(args, operation=None):
    if not args.url:
        raise CliError("URL_REQUIRED", "Set SILVER_CLI_URL or pass --url for the intended server.", 2)
    username = os.environ.get("SILVER_CLI_USERNAME")
    password = os.environ.get("SILVER_CLI_PASSWORD")
    if bool(username) != bool(password):
        raise CliError("CREDENTIALS", "Set both SILVER_CLI_USERNAME and SILVER_CLI_PASSWORD, or neither.", 2)
    client = Client(args.url, timeout=args.timeout, allow_http=args.allow_http)
    if username and (operation is None or operation.name not in {"auth.login", "auth.register"}):
        client.login(username, password)
    return client


def main(argv=None):
    secrets = {os.environ.get("SILVER_CLI_PASSWORD", "")}
    try:
        args = _parser().parse_args(argv)
        if getattr(args, "high_level", False):
            result, code = execute(args, secrets), 0
            if isinstance(result, tuple):
                result, code = result
            _emit(result, secrets)
            return code
        if args.command == "list":
            rows = [operation.to_dict() for operation in operations()
                    if args.group is None or operation.name.startswith(args.group + ".")]
            _emit(_ok({"operations": rows, "count": len(rows)}), secrets)
            return 0
        operation = get_operation(args.operation)
        if args.command == "describe":
            _emit(_ok(operation.to_dict()), secrets)
            return 0
        path, kwargs = _prepare(args, operation)
        secrets.update(_collect_secrets(kwargs.get("json_body")))
        secrets.update(value for key, value in kwargs.get("form", []) if _sensitive(key))
        secrets.update(value for key, value in kwargs["query"] if _sensitive(key))
        if args.dry_run:
            _emit(_ok({"dry_run": True, "operation": operation.name, "method": operation.method,
                       "path": path, "mutating": operation.mutating,
                       "query_fields": [key for key, value in kwargs["query"]],
                       "body_fields": list((kwargs.get("json_body") or {}).keys()),
                       "form_fields": [key for key, value in kwargs.get("form", [])],
                       "file_fields": [key for key, value in kwargs.get("files", [])]}), secrets)
            return 0
        if operation.mutating and not getattr(args, "confirm", False):
            raise CliError("CONFIRM_REQUIRED", "Mutating operations require --confirm; inspect --dry-run first.", 2)
        client = _client(args, operation)
        if args.command == "wait":
            return _wait(client, operation, path, kwargs, args, secrets)
        if operation.response_kind == "stream":
            events = client.request(operation.method, path, stream=True, **kwargs)
            try:
                for count, event in enumerate(events, 1):
                    _emit(_ok(event), secrets)
                    if event.get("event") == "end":
                        if isinstance(event.get("data"), dict) and event["data"].get("reason") == "stream-timeout":
                            raise CliError("STREAM_TIMEOUT", "Server requested stream reconnection; resume with the last event id.", 9)
                        return 0
                    if count >= args.max_events:
                        raise CliError("STREAM_LIMIT", "Event limit reached; resume using the last event id.", 9)
            finally:
                events.close()
            raise CliError("STREAM_CLOSED", "Stream closed without an end event; resume using the last event id.", 6)
        response = client.request(operation.method, path, **kwargs)
        _emit(response, secrets)
        return 0
    except CliError as exc:
        _emit({"success": False, "data": None, "error": {"code": exc.code,
               "message": exc.message, "details": getattr(exc, "details", None)},
               "request_id": getattr(exc, "request_id", None), "status": getattr(exc, "status", None)}, secrets)
        return exc.exit_code
    except (ValueError, OSError) as exc:
        _emit({"success": False, "data": None, "error": {"code": "INVALID_INPUT",
               "message": "Invalid input or inaccessible local file; use --help and describe."}, "request_id": None}, secrets)
        return 2
    except KeyboardInterrupt:
        _emit({"success": False, "data": None, "error": {"code": "INTERRUPTED",
               "message": "Client interrupted; remote work may continue."}, "request_id": None}, secrets)
        return 130
