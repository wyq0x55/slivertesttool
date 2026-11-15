"""What the variable-curve viewer knows about a task's signals and verdict.

Three things are derived for one attempt, all from evidence of *that* attempt:

* the **judge timeline** — steps with start/end time and result, and every
  judged check with its variable, expectation, observation and time, parsed
  from the attempt's ``jdgrslt.log``;
* the **signal dictionary** — per variable path its display name and role
  (stimulus input / checked output), from the pinned test procedure of a
  sealed attempt plus the judge's monitoring targets;
* **value labels** — per variable the constants (macro names) a case writes
  to or expects of it, mapped value -> names, from the pinned constants and
  the judge's expected values.

Nothing here is inferred from signal values or from the live matrix.
"""
from __future__ import annotations

import re

from . import run_evidence_service as evidence

MAX_JDG_BYTES = 16 * 1024 * 1024
_TARGET_RE = re.compile(r"^(?P<name>.*?)\s*\(\s*(?P<var>[^()\s]+)\s*\)\s*$")
_MACRO_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\((-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?))\)")
_OBSERVED_RE = re.compile(r"\(\s*(-?[\d.]+(?:[eE][-+]?\d+)?)\s*\)\s*$")
_LABEL_RE = re.compile(r"^(?P<label>.*?)\s*\(\s*\(")


def _number(text):
    try:
        value = int(text, 16) if text.lower().startswith(("0x", "-0x")) else float(text)
    except ValueError:
        return None
    return int(value) if float(value).is_integer() else value


def _key(value):
    """Value labels are keyed by the canonical number text the client renders."""
    number = _number(str(value)) if not isinstance(value, (int, float)) else value
    if number is None:
        return None
    return str(int(number)) if float(number).is_integer() else repr(float(number))


# --------------------------------------------------------------------------- #
# Judge timeline
# --------------------------------------------------------------------------- #
def _check(raw):
    target = _TARGET_RE.match(raw.get("target") or "")
    expected = raw.get("expected") or ""
    label = _LABEL_RE.match(expected)
    observed = _OBSERVED_RE.search(raw.get("observed") or "")
    return {
        "ok": raw["ok"],
        "name": (target.group("name") if target else raw.get("target") or "").strip(),
        "var": target.group("var") if target else None,
        "expected": expected,
        "expected_label": (label.group("label").strip() if label else expected),
        "expected_values": [{"name": name, "value": _number(value)}
                            for name, value in _MACRO_RE.findall(expected)],
        "observed": raw.get("observed"),
        "observed_value": _number(observed.group(1)) if observed else None,
        "timing": raw.get("timing"),
        "at": raw.get("at"),
        "subroutine": raw.get("subroutine"),
        "subroutine_step": raw.get("subroutine_step"),
    }


def timeline(task, run_count=None, *, live=False):
    """Structured verdict of one attempt; ``available`` is False without a log."""
    from ..runners.test_runner import extract_case_section, parse_step_results, parse_verdict_text
    try:
        if live:
            path = _live_jdgrslt(task)
        else:
            path = evidence.result_file(task, "jdgrslt.log", max_bytes=MAX_JDG_BYTES, run_count=run_count)
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return {"available": False, "verdict": None, "steps": [], "first_failure": None}
    section = extract_case_section(text, task.test_id)
    steps, previous = [], 0.0
    for raw in parse_step_results(section):
        end = raw["at"] and float(raw["at"].rstrip("s"))
        title = " ".join(raw["description"][:2])[:160] or f"Step {raw['step']}"
        steps.append({"step": raw["step"], "phase": raw.get("phase"), "result": raw["result"],
                      "start": previous, "end": end, "title": title,
                      "checks": [_check(c) for c in raw["checks"]]})
        if end is not None:
            previous = end
    failure = next((s for s in steps if s["result"] == "FAIL"), None)
    first = None
    if failure is not None:
        bad = next((c for c in failure["checks"] if not c["ok"]), None)
        first = {"step": failure["step"], "start": failure["start"], "end": failure["end"],
                 "var": bad and bad["var"], "at": (bad and bad["at"]) or failure["end"]}
    return {"available": True, "verdict": parse_verdict_text(section) if section else None,
            "steps": steps, "first_failure": first}


def _live_jdgrslt(task):
    from .task_curves_service import _live_path
    return _live_path(task, task.run_count or 1).with_name("jdgrslt.log")


# --------------------------------------------------------------------------- #
# Signal dictionary and value labels
# --------------------------------------------------------------------------- #
def _pinned(task, run_count):
    """Approved procedure of a sealed attempt, or None for a legacy task."""
    try:
        manifest = evidence.read_manifest(task, run_count)
    except (OSError, ValueError):
        return None
    return manifest.get("approved_inputs") or None


def dictionary(task, run_count=None, judge=None):
    """``{var: {name, roles, labels: {value: [names]}}}`` for one attempt."""
    from .lanmatrix.silver_json_export import _blank_cell, _signal_pairs, _split_cell
    signals: dict[str, dict] = {}

    def entry(var):
        return signals.setdefault(var, {"name": "", "roles": [], "labels": {}})

    def role(var, name, kind):
        item = entry(var)
        if name and not item["name"]:
            item["name"] = name
        if kind not in item["roles"]:
            item["roles"].append(kind)

    def label(var, value, name):
        key = _key(value)
        if key is None or not name:
            return
        names = entry(var)["labels"].setdefault(key, [])
        if name not in names:
            names.append(name)

    pinned = _pinned(task, run_count)
    if pinned:
        constants = (pinned.get("constants") or {}).get("constants") or {}
        bodies = [pinned.get("test") or {}, *(pinned.get("libraries") or {}).values()]
        for body in bodies:
            if not isinstance(body, dict):
                continue
            ins = _signal_pairs(body.get("input_signals"))
            exps = _signal_pairs(body.get("expected_signals"))
            for name, var in ins:
                if var.strip():
                    role(var.strip(), name.strip(), "input")
            for name, var in exps:
                if var.strip():
                    role(var.strip(), name.strip(), "check")
            for step in body.get("steps") if isinstance(body.get("steps"), list) else []:
                if not isinstance(step, dict):
                    continue
                for pairs, cells in ((ins, step.get("inputs") or []), (exps, step.get("expecteds") or [])):
                    for (_, var), cell in zip(pairs, cells):
                        if not var.strip() or _blank_cell(cell):
                            continue
                        jname, ident = _split_cell(cell)
                        const = constants.get(ident)
                        if isinstance(const, dict):
                            label(var.strip(), const.get("value"), ident)
                            if const.get("name_ja"):
                                label(var.strip(), const.get("value"), str(const["name_ja"]))
                        elif jname and _number(ident) is not None:
                            label(var.strip(), ident, jname)
    judge = judge if judge is not None else timeline(task, run_count)
    for step in judge.get("steps", []):
        for check in step["checks"]:
            if not check["var"]:
                continue
            role(check["var"], check["name"], "check")
            for macro in check["expected_values"]:
                label(check["var"], macro["value"], macro["name"])
            if len(check["expected_values"]) == 1 and check["expected_label"]:
                label(check["var"], check["expected_values"][0]["value"], check["expected_label"])
    return {"source": "pinned" if pinned else "judge", "signals": signals}
