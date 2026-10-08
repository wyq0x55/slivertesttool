"""Derive CLI operations from route source without importing the application."""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path


ROUTE_ROOT = Path(__file__).resolve().parents[1] / "app" / "routes" / "lanmatrix"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    summary: str
    path_params: dict[str, str]
    query_params: list[str]
    body_fields: list[str]
    form_fields: list[str]
    file_fields: list[str]
    response_kind: str
    mutating: bool

    def to_dict(self):
        result = asdict(self)
        result["field_hints_only"] = True
        result["validation"] = "Server validation and permissions remain authoritative. Nested/conditional fields may not be listed."
        return result


def _constant(node, bindings=None):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return (bindings or {}).get(node.id)
    return None


def _prefix(module, trees, visited=None):
    visited = set() if visited is None else visited
    if module in visited:
        raise ValueError("Circular blueprint definition.")
    visited.add(module)
    for node in trees[module].body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "bp" for target in node.targets):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "Blueprint":
                return next(_constant(keyword.value) for keyword in node.value.keywords if keyword.arg == "url_prefix")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in trees:
            if any(alias.name == "bp" and alias.asname in (None, "bp") for alias in node.names):
                return _prefix(node.module, trees, visited)
    raise ValueError("Route has no resolvable blueprint prefix.")


def _reachable(trees):
    pending = ["__init__"]
    found = set()
    while pending:
        module = pending.pop()
        if module in found:
            continue
        found.add(module)
        for node in trees[module].body:
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                candidates = [node.module] if node.module else [alias.name for alias in node.names]
                pending.extend(name for name in candidates if name in trees and name not in found)
    return found


def _hints(function, functions, visited=None, bindings=None):
    visited = set() if visited is None else visited
    if function.name in visited:
        return {"query": set(), "body": set(), "form": set(), "files": set()}, set(), set()
    visited = visited | {function.name}
    hints = {"query": set(), "body": set(), "form": set(), "files": set()}
    strings = set()
    calls = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "body":
            value = _constant(node.slice, bindings)
            if isinstance(value, str):
                hints["body"].add(value)
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name:
            calls.add(name)
        if name in functions:
            child = functions[name]
            bound = {param.arg: _constant(argument, bindings)
                     for param, argument in zip(child.args.args, node.args)}
            bound.update({keyword.arg: _constant(keyword.value, bindings)
                          for keyword in node.keywords if keyword.arg})
            child_hints, child_strings, child_calls = _hints(child, functions, visited, bound)
            for kind in hints:
                hints[kind].update(child_hints[kind])
            strings.update(child_strings)
            calls.update(child_calls)
        if not node.args:
            continue
        value = _constant(node.args[0], bindings)
        if not isinstance(value, str):
            continue
        if name in {"arg_int", "arg_str", "arg_json", "arg_date"}:
            hints["query"].add(value)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "getlist"}:
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "body":
                hints["body"].add(value)
            if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == "request":
                kind = {"args": "query", "form": "form", "files": "files"}.get(owner.attr)
                if kind:
                    hints[kind].add(value)
    return hints, strings, calls


@lru_cache(maxsize=1)
def operations():
    trees = {path.stem: ast.parse(path.read_text(encoding="utf-8-sig"))
             for path in ROUTE_ROOT.glob("*.py")}
    result = []
    for module in sorted(_reachable(trees)):
        functions = {node.name: node for node in trees[module].body if isinstance(node, ast.FunctionDef)}
        for function in functions.values():
            routes = []
            for decorator in function.decorator_list:
                if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "bp"):
                    continue
                verb = decorator.func.attr
                if verb not in METHODS | {"route"}:
                    continue
                suffix = _constant(decorator.args[0])
                if not isinstance(suffix, str):
                    raise ValueError("Dynamic routes require an explicit CLI catalogue adapter.")
                methods = [verb.upper()]
                if verb == "route":
                    methods = ["GET"]
                    for keyword in decorator.keywords:
                        if keyword.arg == "methods":
                            methods = ast.literal_eval(keyword.value)
                routes.extend((method, _prefix(module, trees) + suffix)
                              for method in methods if method not in {"HEAD", "OPTIONS"})
            if not routes:
                continue
            hints, strings, calls = _hints(function, functions)
            kind = "json"
            if "text/event-stream" in strings:
                kind = "stream"
            elif "send_file" in calls or any("text/csv" in string for string in strings):
                kind = "download"
            for method, path in routes:
                name = module + "." + function.name
                if len(routes) > 1:
                    name += "." + method.lower()
                result.append(Operation(
                    name=name, method=method, path=path,
                    summary=ast.get_docstring(function) or function.name.replace("_", " "),
                    path_params={name: converter or "string" for converter, name in re.findall(r"<(?:(\w+):)?(\w+)>", path)},
                    query_params=sorted(hints["query"]), body_fields=sorted(hints["body"]),
                    form_fields=sorted(hints["form"]), file_fields=sorted(hints["files"]),
                    response_kind=kind, mutating=method not in {"GET", "HEAD", "OPTIONS"},
                ))
    if len({operation.name for operation in result}) != len(result):
        raise ValueError("Duplicate CLI operation names.")
    return sorted(result, key=lambda operation: operation.name)


def get_operation(name):
    for operation in operations():
        if operation.name == name:
            return operation
    raise ValueError("Unknown operation; use list to discover supported operations.")
