# coding: UTF-8
"""Pure, Silver-SDK-free step and constant expression parser.

Shared directly by both the runtime ``silver_json_runner.py`` (inside Silver's
embedded Python) and the web/worker ``run_validation_service.py`` (pre-run
validation), eliminating source-file ``ast.parse`` + ``exec`` extraction.
"""

from __future__ import annotations

import ast
import operator as _operator
import re
import unicodedata

try:
    from .silver_test_framework import Assign, Call, Check, Step, SubCall
except ImportError:
    from silver_test_framework import Assign, Call, Check, Step, SubCall


class MagicList(list):
    """A list that compares equal to any element it contains."""

    def __eq__(self, other):
        return other in self

    def __ne__(self, other):
        return other not in self

    __hash__ = None


def _const_value(entry):
    """A constants.json entry is either a bare value or ``{"value": v, ...}``."""
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return entry


def _const_name_ja(consts, name):
    """The Japanese display name (name_ja) of a constant, used as default check desc."""
    entry = consts.get(name)
    if isinstance(entry, dict):
        return entry.get("name_ja", "")
    return ""


_BIN_OPS = {
    ast.Add: _operator.add,
    ast.Sub: _operator.sub,
    ast.Mult: _operator.mul,
    ast.Div: _operator.truediv,
    ast.Mod: _operator.mod,
    ast.Pow: _operator.pow,
    ast.FloorDiv: _operator.floordiv,
    ast.BitAnd: _operator.and_,
    ast.BitOr: _operator.or_,
    ast.BitXor: _operator.xor,
    ast.LShift: _operator.lshift,
    ast.RShift: _operator.rshift,
}
_UNARY_OPS = {
    ast.UAdd: _operator.pos,
    ast.USub: _operator.neg,
    ast.Invert: _operator.invert,
}

_OP_PREFIXES = (
    (">=", (">=", "=>", "\u2265", "\u2267", "\uff1e\uff1d", "\uff1e=")),
    ("<=", ("<=", "=<", "\u2264", "\u2266", "\uff1c\uff1d", "\uff1c=")),
    ("!=", ("!=", "\u2260", "<>", "\uff1c\uff1e", "\uff01\uff1d", "\uff01=")),
    ("==", ("==", "\uff1d\uff1d", "=", "\uff1d")),
    (">", (">", "\uff1e")),
    ("<", ("<", "\uff1c")),
)

_INTERVAL_RE = re.compile(
    r"^([\[\(])\s*(.*?)\s*[,\u3001\uff0c]\s*(.*?)\s*([\]\)])$", re.S
)
_INF_LO = ("", "-inf", "-\u221e", "\u221e-")
_INF_HI = ("", "inf", "+inf", "\u221e", "+\u221e")
_LABEL_EXPR_RE = re.compile(r"^[^\(\)（）]+[\(（](.+)[\)）]$", re.DOTALL)


def _split_operator(text):
    """Split a leading relational operator off an expected cell."""
    s = text.strip()
    for canon, prefixes in _OP_PREFIXES:
        for p in prefixes:
            if s.startswith(p):
                return canon, s[len(p):].strip()
    return "==", s


def _looks_like_interval(text):
    """True when ``text`` (as-is) is bracketed like an interval cell."""
    s = unicodedata.normalize("NFKC", str(text).strip())
    return bool(_INTERVAL_RE.match(s))


def _param_names(spec):
    """Ordered formal-parameter names declared by a subroutine spec."""
    return [
        p["name"]
        for p in spec.get("params", [])
        if isinstance(p, dict) and p.get("name")
    ]


def _resolve_arg(raw, consts):
    """Call args: strings that name a constant resolve to it, else literal."""
    if isinstance(raw, str) and raw in consts:
        return _const_value(consts[raw])
    return raw


def make_parser(*, bin_ops=None, unary_ops=None) -> dict:
    """Build a step-parser function bundle over the given AST operator tables."""
    active_bin_ops = _BIN_OPS if bin_ops is None else bin_ops
    active_unary_ops = _UNARY_OPS if unary_ops is None else unary_ops

    def eval_node(node, consts):
        if isinstance(node, ast.Expression):
            return eval_node(node.body, consts)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Num):  # pragma: no cover - legacy Python
            return node.n
        if isinstance(node, ast.BinOp) and type(node.op) in active_bin_ops:
            return active_bin_ops[type(node.op)](
                eval_node(node.left, consts),
                eval_node(node.right, consts),
            )
        if isinstance(node, ast.UnaryOp) and type(node.op) in active_unary_ops:
            return active_unary_ops[type(node.op)](eval_node(node.operand, consts))
        if isinstance(node, ast.Name):
            if node.id in consts:
                return _const_value(consts[node.id])
            raise KeyError("Unknown constant name in JSON: %r" % node.id)
        raise ValueError("Unsupported expression element: %r" % ast.dump(node))

    def eval_expr(text, consts):
        tree = ast.parse(text, mode="eval")
        return eval_node(tree, consts)

    def parse_interval(text, consts):
        s = unicodedata.normalize("NFKC", str(text).strip())
        m = _INTERVAL_RE.match(s)
        if not m:
            return None
        lb, lo_s, hi_s, rb = (
            m.group(1),
            m.group(2).strip(),
            m.group(3).strip(),
            m.group(4),
        )
        lo = None if lo_s.lower() in _INF_LO else eval_expr(lo_s, consts)
        hi = None if hi_s.lower() in _INF_HI else eval_expr(hi_s, consts)
        return (lo, hi, lb == "[", rb == "]")

    def resolve_single(s, consts):
        if s in consts:
            return _const_value(consts[s])
        try:
            return eval_expr(s, consts)
        except Exception:
            pass
        m = _LABEL_EXPR_RE.match(s)
        if m:
            inner = unicodedata.normalize("NFKC", m.group(1).strip())
            if inner in consts:
                return _const_value(consts[inner])
            try:
                return eval_expr(inner, consts)
            except Exception:
                pass
        raise KeyError("Unknown constant name in JSON: %r" % s)

    def resolve_value(raw, consts):
        if not isinstance(raw, str):
            return raw
        s = raw.strip()
        if s in consts:
            return _const_value(consts[s])
        if "," in s:
            parts = [p.strip() for p in s.split(",") if p.strip()]
            if len(parts) > 1:
                return MagicList(resolve_single(p, consts) for p in parts)
        return resolve_single(s, consts)

    def build_steps_fn(spec, consts):
        default_timeout = spec.get("default_timeout", 5)
        steps = []
        for s in spec["steps"]:
            inputs = [
                Assign(a["var"], resolve_value(a["value"], consts), param=a.get("param"))
                for a in s.get("inputs", [])
            ]
            actions = []
            for a in s.get("actions", []):
                if "subroutine" in a:
                    actions.append(
                        SubCall(
                            name=a["subroutine"],
                            args=[resolve_value(x, consts) for x in a.get("args", [])],
                        )
                    )
                else:
                    actions.append(
                        Call(
                            func=a["call"],
                            args=[_resolve_arg(x, consts) for x in a.get("args", [])],
                            kind=a.get("kind", "auto"),
                            store=a.get("store"),
                        )
                    )
            checks = []
            for c in s.get("checks", []):
                exp_raw = c["expected"]
                if isinstance(exp_raw, str) and _looks_like_interval(exp_raw):
                    op = "in"
                    exp_name = exp_raw.strip()
                    exp_value = parse_interval(exp_raw, consts)
                elif isinstance(exp_raw, str):
                    op, expr = _split_operator(exp_raw)
                    exp_name = expr
                    exp_value = resolve_value(expr, consts)
                else:
                    op, exp_name = "==", str(exp_raw)
                    exp_value = resolve_value(exp_raw, consts)
                exp_desc = c.get("desc")
                if exp_desc is None:
                    exp_desc = _const_name_ja(consts, exp_name)
                checks.append(
                    Check(
                        var=c["var"],
                        label=c["label"],
                        exp_name=exp_name,
                        exp_value=exp_value,
                        exp_desc=exp_desc,
                        timing=c.get("timing", "任意"),
                        op=op,
                        param=c.get("param"),
                    )
                )
            steps.append(
                Step(
                    no=s["no"],
                    header_log=s.get("header_log", True),
                    category=s.get("category"),
                    comment=s.get("comment"),
                    inputs=inputs,
                    actions=actions,
                    checks=checks,
                    timeout=s.get("timeout", default_timeout),
                    method=s.get("method", "reach"),
                    watch_ms=s.get("watch_ms", 0),
                )
            )
        return steps

    return {
        "MagicList": MagicList,
        "_const_value": _const_value,
        "_const_name_ja": _const_name_ja,
        "_split_operator": _split_operator,
        "_looks_like_interval": _looks_like_interval,
        "_parse_interval": parse_interval,
        "_eval_node": eval_node,
        "_eval_expr": eval_expr,
        "_resolve_single": resolve_single,
        "_resolve_value": resolve_value,
        "_param_names": _param_names,
        "_resolve_arg": _resolve_arg,
        "build_steps": build_steps_fn,
    }


_DEFAULT_PARSER = make_parser()
_eval_node = _DEFAULT_PARSER["_eval_node"]
_eval_expr = _DEFAULT_PARSER["_eval_expr"]
_parse_interval = _DEFAULT_PARSER["_parse_interval"]
_resolve_single = _DEFAULT_PARSER["_resolve_single"]
_resolve_value = _DEFAULT_PARSER["_resolve_value"]
build_steps = _DEFAULT_PARSER["build_steps"]
