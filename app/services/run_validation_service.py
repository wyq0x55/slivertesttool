"""Validate generated documents with the vendored runner's pure parser."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
import unicodedata
from functools import partial
from functools import lru_cache
from pathlib import Path

from ..runners.silver_json import silver_test_framework as framework


class ConversionError(ValueError):
    """An approved procedure cannot be executed without changing its meaning."""


def _bounded_binary(kind, operation, left, right):
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        raise ConversionError("Expression arithmetic requires numeric operands")
    _finite((left, right), "expression")
    if kind is ast.Pow and abs(left) > 1 and right > 0 and math.log2(abs(left)) * right > 4096:
        raise ConversionError("Expression exponent exceeds the numeric budget")
    if kind is ast.LShift and (not isinstance(right, int) or right < 0 or right > 4096
                               or isinstance(left, int) and left.bit_length() + right > 4096):
        raise ConversionError("Expression shift exceeds the numeric budget")
    result = operation(left, right)
    if isinstance(result, complex):
        raise ConversionError("Complex expression values are not executable")
    _finite(result, "expression")
    return result


@lru_cache(maxsize=1)
def _parser():
    root = Path(__file__).resolve().parents[1] / "runners" / "silver_json"
    functions = {
        "_const_value", "_const_name_ja", "_split_operator", "_looks_like_interval",
        "_parse_interval", "_eval_node", "_eval_expr", "_resolve_single",
        "_resolve_value", "_resolve_arg", "build_steps", "_param_names",
    }
    assignments = {"_BIN_OPS", "_UNARY_OPS", "_OP_PREFIXES", "_INTERVAL_RE",
                   "_INF_LO", "_INF_HI"}
    namespace = {"ast": ast, "_operator": operator, "re": re,
                 "unicodedata": unicodedata,
                 **{name: getattr(framework, name)
                    for name in ("Assign", "Call", "SubCall", "Check", "Step")}}
    builtin_tree = ast.parse((root / "framework_builtins.py").read_text(encoding="utf-8"))
    magic = next(node for node in builtin_tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "MagicList")
    exec(compile(ast.Module(body=[magic], type_ignores=[]), str(root), "exec"), namespace)
    tree = ast.parse((root / "silver_json_runner.py").read_text(encoding="utf-8"))
    selected = [node for node in tree.body
                if (isinstance(node, ast.FunctionDef) and node.name in functions)
                or (isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in assignments
                    for target in node.targets))]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(root), "exec"), namespace)
    namespace["_BIN_OPS"] = {kind: partial(_bounded_binary, kind, operation)
                             for kind, operation in namespace["_BIN_OPS"].items()}
    return namespace


def _finite(value, label):
    if isinstance(value, int) and value.bit_length() > 4096:
        raise ConversionError(f"{label}: integer exceeds the numeric budget")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConversionError(f"{label}: value must be finite")
    if isinstance(value, (dict, list, tuple)):
        for child in value.values() if isinstance(value, dict) else value:
            _finite(child, label)


def _duration(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConversionError(f"{label}: duration must be non-negative numeric seconds")
    _finite(value, label)


def validate_documents(testcase: dict, constants: dict, library: dict) -> list:
    """Return actual Step objects; raise ConversionError before any execution."""
    parser = _parser()
    try:
        _finite((testcase, constants, library), "documents")
        consts = constants["constants"]
        subs = library["subroutines"]
        if not isinstance(consts, dict) or not isinstance(subs, dict):
            raise ConversionError("constants and subroutines must be objects")
        if not testcase.get("steps"):
            raise ConversionError("testcase must contain at least one step")
        graph = {}
        parsed = {}
        for name, spec in {"<testcase>": testcase, **subs}.items():
            if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list):
                raise ConversionError(f"{name}: steps must be an array")
            _duration(spec.get("default_timeout", 5), name)
            names = parser["_param_names"](spec)
            if len(names) != len(set(names)):
                raise ConversionError(f"{name}: duplicate formal parameter")
            for parameter in spec.get("params", []):
                if "default" in parameter:
                    _finite(parser["_resolve_value"](parameter["default"], consts), name)
            edges = set()
            numbers = set()
            for step in spec["steps"]:
                number = step.get("no")
                if (isinstance(number, bool) or not isinstance(number, (int, float))
                        or number < 1 or number != int(number) or number in numbers):
                    raise ConversionError(f"{name}: step numbers must be unique positive integers")
                numbers.add(number)
                _duration(step.get("timeout", spec.get("default_timeout", 5)), name)
                _duration(step.get("watch_ms", 0), name)
                if step.get("method", "reach") not in ("watch", "reach"):
                    raise ConversionError(f"{name}: unknown timing method")
                for signal in step.get("inputs", []) + step.get("checks", []):
                    if not isinstance(signal.get("var"), str) or not signal["var"].strip():
                        raise ConversionError(f"{name}: signal path is required")
                    if signal.get("param") and signal["param"] not in names:
                        raise ConversionError(f"{name}: unknown formal parameter")
                for action in step.get("actions", []):
                    target = action.get("subroutine")
                    if target not in subs:
                        raise ConversionError(f"{name}: unknown subroutine {target!r}")
                    args = action.get("args", [])
                    if not isinstance(args, list) or len(args) > len(parser["_param_names"](subs[target])):
                        raise ConversionError(f"{name}: incorrect arguments for {target}")
                    edges.add(target)
            for target in spec.get("pre_init", {}).get("init_subroutines", []):
                if target not in subs or subs[target].get("kind") != "init":
                    raise ConversionError(f"{name}: unknown init subroutine {target!r}")
                edges.add(target)
            graph[name] = edges
            steps = parser["build_steps"](spec, consts)
            for step in steps:
                _finite([entry.value for entry in step.inputs], name)
                _finite([entry.exp_value for entry in step.checks], name)
                _finite([entry.args for entry in step.actions], name)
                for check in step.checks:
                    if check.op == "in":
                        lower, upper, include_lower, include_upper = check.exp_value
                        if lower is not None and upper is not None and (
                                lower > upper or (lower == upper and not (include_lower and include_upper))):
                            raise ConversionError(f"{name}: empty or reversed interval")
            parsed[name] = steps
        visited = set()
        active = set()

        def visit(name):
            if name in active:
                raise ConversionError(f"recursive subroutine: {name}")
            if name in visited:
                return
            active.add(name)
            for target in graph[name]:
                visit(target)
            active.remove(name)
            visited.add(name)

        visit("<testcase>")
        return parsed["<testcase>"]
    except ConversionError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError, RecursionError) as exc:
        raise ConversionError(f"Runner document validation failed: {exc}") from exc


def validate_run_directory(case_dir: Path) -> None:
    """Check one generated case directory, without importing the Silver API."""
    cases = list(case_dir.glob("testcase_*.json"))
    if len(cases) != 1:
        raise ConversionError("Exactly one generated testcase is required")
    validate_documents(json_load(cases[0]), json_load(case_dir / "constants.json"), json_load(case_dir / "lib.json"))


def json_load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConversionError(f"Invalid runner document {Path(path).name}: {exc}") from exc
