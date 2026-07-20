"""Build the three ``silver_json_runner`` input documents from DB rows.

The platform stores a project's test procedure entirely in the graphical
step-table editor, persisted as JSON on the ``steps`` (test sheet) and
``lib_stb`` (lib sheet) fields, with constants living as ``const`` sheet rows.
This module turns that data into the exact JSON the vendored runner
(:mod:`app.runners.silver_json`) consumes:

* ``constants.json`` — ``{"constants": {NAME: {value, name_ja, remark}}}``
* ``lib.json``       — ``{"subroutines": {NAME: {kind, [default_timeout], steps}}}``
* ``testcase_<test_id>.json`` —
  ``{test_case_id, lib_json, default_timeout, pre_init, steps}``

``materialise_run_dir`` writes all three next to a freshly-copied set of runner
framework files, producing a self-contained ``run/<test_id>/`` folder that
Silver can execute in place of the legacy ``judge.py`` flow.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

from ...models import TestItemRow
from ...runners import silver_json

# --------------------------------------------------------------------------- #
# Field keys (kept in sync with ``fields.py`` CONST_FIELDS / LIB_FIELDS)
# --------------------------------------------------------------------------- #
CONST_NAME = "const_name"
CONST_VALUE = "const_value"
CONST_JNAME = "const_jname"
CONST_NOTE = "const_note"

LIB_FUNC = "lib_func"
LIB_NAME = "lib_name"
LIB_ISINIT = "isinit"
LIB_STEPS = "lib_stb"

TEST_ID = "test_id"
TEST_STEPS = "steps"

DEFAULT_TIMEOUT = 5


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_json_field(raw: Any) -> dict:
    """Coerce a stored step/procedure field into a dict.

    The step editor persists a JSON object; depending on the storage path it
    may already be a ``dict`` (JSONB) or a JSON string (text column). Blank /
    unparseable values yield an empty dict so export never crashes on a
    half-authored row.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            data = json.loads(s)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _coerce_const_value(raw: Any) -> Any:
    """Constants are numeric in the reference data but stored as text.

    Return an ``int`` / ``float`` when the text is purely numeric (incl. hex
    ``0x..``), otherwise the original string (a symbolic value is still valid —
    the runner resolves check ``expected`` strings against constant *names*).
    """
    if raw is None:
        return 0
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw
    s = str(raw).strip()
    if s == "":
        return 0
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
    except ValueError:
        pass
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on", "init")


def _steps_list(body: dict) -> list:
    steps = body.get("steps")
    return steps if isinstance(steps, list) else []


# --------------------------------------------------------------------------- #
# Columnar (Excel 手順 table) -> runner-schema conversion
#
# The step editor / Excel importer persist the procedure in a *columnar* shape
# that mirrors the 手順 table 1:1::
#
#     { "input_signals":    [[name, path], ...],
#       "expected_signals": [[name, path], ...],
#       "steps": [ { "no", "purpose", "operation", "subroutine", "args",
#                    "inputs":   [cell, ...],   # one per input signal
#                    "expecteds":[cell, ...],   # one per expected signal
#                    "timing" }, ... ] }
#
# The runner consumes a *per-step* shape instead (see the reference
# ``testcase_TC_APL-*.json``)::
#
#     { "no", "category"=手順目的, "comment"=操作手順,
#       "actions": [{"subroutine": <非初期化 lib>}],
#       "inputs":  [{"var": path, "value": <const 名/リテラル>}],
#       "checks":  [{"var": path, "label": 期待値名,
#                    "expected": <識別子>, "desc": <和名>, "timing"}] }
#
# Value cells encode ``和名(識別子)`` (full- or half-width parens); an expected /
# input cell of ``-`` or blank means "no check / no input on this step".
# --------------------------------------------------------------------------- #
_PAREN_RE = re.compile(r"^(?P<jname>.*?)[（(]\s*(?P<ident>[^（）()]+?)\s*[)）]\s*$")
# An interval / range cell such as "[0,50)" or "(0,50]" is passed through as a
# bare identifier so the paren-splitter above does not strip its brackets.
_INTERVAL_CELL_RE = re.compile(
    r"^[\[\(].*?[,\u3001\uff0c，].*?[\]\)]$", re.S)


def _blank_cell(cell: Any) -> bool:
    """A signal cell with no value: ``None``, empty, or the ``-`` placeholder."""
    if cell is None:
        return True
    return str(cell).strip() in ("", "-")


def _split_cell(cell: Any) -> tuple[str, str]:
    """Split a ``和名(識別子)`` cell into ``(jname, identifier)``.

    Both full-width ``（）`` and half-width ``()`` parentheses are accepted. A
    cell without parentheses is treated as a bare identifier (``jname=''``).
    """
    s = "" if cell is None else str(cell).strip()
    if _INTERVAL_CELL_RE.match(unicodedata.normalize("NFKC", s)):
        return "", s
    m = _PAREN_RE.match(s)
    if m:
        return m.group("jname").strip(), m.group("ident").strip()
    return "", s


# --------------------------------------------------------------------------- #
# 確認タイミング (confirmation timing) parsing
#
# A timing cell is free text that encodes *how* the expected value is judged
# (the legacy ``JudgeMethod``) plus an optional 規定時間 (duration):
#
#   DEFAULT   '-'      時間を指定しない                     -> reach (default timeout)
#   KEEP      'WATCH'  規定時間、期待値を維持することを確認 -> watch (hold for N)
#   UNTIL     'WAIT'   規定時間までに変化することを確認     -> reach within N
#   IMMEDIATE 'JUDGE'  即座に期待値に変化することを確認     -> reach immediately (0)
#
# The runner already consumes per-step ``method`` / ``timeout`` / ``watch_ms``,
# so parsing happens here at export time; the human timing text is preserved on
# each check for judge-compatible ``確認タイミング：`` output.
# --------------------------------------------------------------------------- #
JUDGE_DEFAULT = "-"
JUDGE_KEEP = "WATCH"
JUDGE_UNTIL = "WAIT"
JUDGE_IMMEDIATE = "JUDGE"

# (keyword, judge method), matched against the NFKC-normalised cell text.
# Ordered longest / most-specific first so that e.g. ``成立するまで`` is DEFAULT
# even though it ends in the UNTIL keyword ``まで`` and ``以内に`` wins over
# ``以内``.
_TIMING_KEYWORDS: list[tuple[str, str]] = [
    ("成立するまで", JUDGE_DEFAULT),
    ("維持", JUDGE_KEEP), ("継続", JUDGE_KEEP), ("監視", JUDGE_KEEP),
    ("キープ", JUDGE_KEEP),
    ("以内に", JUDGE_UNTIL), ("以内", JUDGE_UNTIL),
    ("までに", JUDGE_UNTIL), ("まで", JUDGE_UNTIL),
    ("即座", JUDGE_IMMEDIATE), ("即時", JUDGE_IMMEDIATE),
]
# Bare-dash / blank tokens (post-NFKC) that also mean DEFAULT (任意).
_DASH_TOKENS = {"", "-", "―", "ー", "─", "‐"}

_TIMING_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


class TimingInfo:
    """Parsed 確認タイミング: judge ``method`` + optional duration + display."""

    __slots__ = ("method", "seconds", "display")

    def __init__(self, method: str, seconds: Optional[float], display: str):
        self.method = method
        self.seconds = seconds
        self.display = display


def _extract_seconds(text: str) -> Optional[float]:
    """Pull the 規定時間 out of a timing cell, in seconds.

    ``ms`` / ``ミリ秒`` are treated as milliseconds; everything else (秒 / s /
    bare number) as seconds. Returns ``None`` when no number is present.
    """
    m = _TIMING_NUM_RE.search(text)
    if not m:
        return None
    val = float(m.group(1))
    low = text.lower()
    if "ms" in low or "msec" in low or "ミリ" in text:
        return val / 1000.0
    return val


def _parse_timing(cell: Any) -> TimingInfo:
    """Parse a 確認タイミング cell into a :class:`TimingInfo`.

    A blank / dash cell means 任意 (any timing) -> DEFAULT.
    """
    import unicodedata

    raw = "" if cell is None else str(cell).strip()
    norm = unicodedata.normalize("NFKC", raw)
    if norm in _DASH_TOKENS:
        return TimingInfo(JUDGE_DEFAULT, None, "任意")

    method = JUDGE_DEFAULT
    for kw, m in _TIMING_KEYWORDS:
        if kw in norm:
            method = m
            break
    seconds = _extract_seconds(norm)
    return TimingInfo(method, seconds, raw or "任意")


def _timing_step_fields(info: TimingInfo) -> dict:
    """Map a :class:`TimingInfo` onto the runner's per-step judge fields."""
    if info.method == JUDGE_KEEP:
        out: dict[str, Any] = {"method": "watch"}
        if info.seconds is not None:
            out["watch_ms"] = int(round(info.seconds * 1000))
        return out
    if info.method == JUDGE_IMMEDIATE:
        return {"method": "reach", "timeout": 0}
    # UNTIL and DEFAULT both wait until the value is reached; UNTIL carries an
    # explicit deadline, DEFAULT falls back to the case default_timeout.
    out = {"method": "reach"}
    if info.seconds is not None:
        out["timeout"] = info.seconds
    return out


def _norm_timing(cell: Any) -> str:
    """確認タイミング display text; a blank / ``-`` cell means 任意 (any timing)."""
    return _parse_timing(cell).display


def _signal_pairs(signals: Any) -> list[tuple[str, str]]:
    """Normalise an ``input_signals`` / ``expected_signals`` list to (name, path)."""
    out: list[tuple[str, str]] = []
    for sig in signals if isinstance(signals, list) else []:
        if isinstance(sig, (list, tuple)):
            name = "" if len(sig) < 1 or sig[0] is None else str(sig[0])
            path = "" if len(sig) < 2 or sig[1] is None else str(sig[1])
        elif isinstance(sig, dict):
            name = str(sig.get("name") or "")
            path = str(sig.get("path") or "")
        else:
            name, path = str(sig or ""), ""
        out.append((name, path))
    return out


def _step_subroutine(step: dict) -> str:
    sub = step.get("subroutine")
    return str(sub).strip() if sub not in (None, "") else ""


def _convert_step(step: dict,
                  in_sigs: list[tuple[str, str]],
                  exp_sigs: list[tuple[str, str]],
                  *, action_sub: Optional[str] = None) -> dict:
    """Turn one columnar step into a runner ``steps[]`` entry.

    Key order matches the reference JSON: ``no, category, comment, actions,
    inputs, checks``. Empty sections are omitted entirely.
    """
    out: dict[str, Any] = {"no": step.get("no")}

    purpose = step.get("purpose")
    if purpose not in (None, ""):
        out["category"] = purpose
    operation = step.get("operation")
    if operation not in (None, ""):
        out["comment"] = operation

    if action_sub:
        out["actions"] = [{"subroutine": action_sub}]

    inputs: list[dict] = []
    in_cells = step.get("inputs") or []
    for j, (_name, path) in enumerate(in_sigs):
        cell = in_cells[j] if j < len(in_cells) else None
        if _blank_cell(cell):
            continue
        _jname, ident = _split_cell(cell)
        inputs.append({"var": path, "value": _coerce_const_value(ident)})
    if inputs:
        out["inputs"] = inputs

    checks: list[dict] = []
    exp_cells = step.get("expecteds") or []
    tinfo = _parse_timing(step.get("timing"))
    for j, (name, path) in enumerate(exp_sigs):
        cell = exp_cells[j] if j < len(exp_cells) else None
        if _blank_cell(cell):
            continue
        jname, ident = _split_cell(cell)
        chk: dict[str, Any] = {"var": path, "label": name, "expected": ident}
        if jname:
            chk["desc"] = jname
        chk["timing"] = tinfo.display
        checks.append(chk)
    if checks:
        out["checks"] = checks
        # The 確認タイミング drives how the whole step is judged (hold vs. reach
        # vs. immediate) plus its 規定時間; attach the runner's per-step fields.
        out.update(_timing_step_fields(tinfo))

    return out


_SAFE_RE = re.compile(r"[^0-9A-Za-z._-]+")


def safe_test_id(test_id: str) -> str:
    """A filesystem-safe token derived from a test id (for the JSON filename)."""
    token = _SAFE_RE.sub("_", (test_id or "").strip()).strip("_")
    return token or "testcase"


def row_test_id(row: TestItemRow) -> str:
    """The logical test id of a ``test`` row: its ``test_id`` field, else case_id."""
    val = row.get_field(TEST_ID)
    if val is None or str(val).strip() == "":
        return (row.case_id or "").strip()
    return str(val).strip()


# --------------------------------------------------------------------------- #
# Document builders
# --------------------------------------------------------------------------- #
def build_constants(const_rows: Iterable[TestItemRow]) -> dict:
    """``const`` sheet rows -> ``constants.json`` document."""
    consts: dict[str, dict] = {}
    for row in const_rows:
        name = row.get_field(CONST_NAME)
        if name is None or str(name).strip() == "":
            continue
        consts[str(name).strip()] = {
            "value": _coerce_const_value(row.get_field(CONST_VALUE)),
            "name_ja": row.get_field(CONST_JNAME) or "",
            "remark": row.get_field(CONST_NOTE) or "",
        }
    return {"constants": consts}


def build_lib(lib_rows: Iterable[TestItemRow]) -> dict:
    """``lib`` sheet rows -> ``lib.json`` document.

    Each row's ``lib_stb`` field carries the same columnar 手順 block as a test
    case; ``isinit`` selects the ``init`` vs ``process`` kind. The subroutine
    name is the row's ``lib_func`` (fallback ``lib_name`` / ``case_id``). The
    columnar steps are converted to the runner's per-step schema so a
    subroutine's ``inputs`` / ``checks`` resolve exactly like a test case's.
    """
    subs: dict[str, dict] = {}
    for row in lib_rows:
        name = (row.get_field(LIB_FUNC) or row.get_field(LIB_NAME)
                or row.case_id or "")
        name = str(name).strip()
        if not name:
            continue
        body = _parse_json_field(row.get_field(LIB_STEPS))
        in_sigs = _signal_pairs(body.get("input_signals"))
        exp_sigs = _signal_pairs(body.get("expected_signals"))
        steps_out: list[dict] = []
        for step in _steps_list(body):
            if not isinstance(step, dict):
                continue
            sub = _step_subroutine(step)
            steps_out.append(
                _convert_step(step, in_sigs, exp_sigs, action_sub=sub or None))
        entry: dict[str, Any] = {
            "kind": "init" if _as_bool(row.get_field(LIB_ISINIT)) else "process",
            "steps": steps_out,
        }
        if "default_timeout" in body:
            entry["default_timeout"] = body["default_timeout"]
        subs[name] = entry
    return {"subroutines": subs}


def build_testcase(test_row: TestItemRow, *,
                   init_names: Iterable[str] = (),
                   lib_json_name: str = "lib.json") -> dict:
    """A ``test`` sheet row -> ``testcase_<id>.json`` document.

    The row's ``steps`` field holds the columnar 手順 block authored in the step
    editor (or imported from Excel). It is converted here to the runner schema::

        { test_case_id, lib_json, default_timeout, pre_init, steps }

    A step whose ``subroutine`` names an *initialisation* library
    (``init_names`` — derived from the lib sheet's ``isinit`` rows) is hoisted
    into ``pre_init`` (its 手順目的 / 操作手順 become ``logs`` and the subroutine
    name goes to ``init_subroutines``) and reduced to a ``{no, header_log}``
    marker in ``steps`` — matching the reference test cases. Every other step
    keeps its 手順目的→category, 操作手順→comment, サブルーチン→actions,
    入力値→inputs and 期待値→checks.
    """
    init_set = {str(n).strip() for n in init_names if str(n).strip()}
    body = _parse_json_field(test_row.get_field(TEST_STEPS))
    in_sigs = _signal_pairs(body.get("input_signals"))
    exp_sigs = _signal_pairs(body.get("expected_signals"))

    steps_out: list[dict] = []
    pre_logs: list[Any] = []
    init_subs: list[str] = []
    for step in _steps_list(body):
        if not isinstance(step, dict):
            continue
        sub = _step_subroutine(step)
        if sub and sub in init_set:
            for val in (step.get("purpose"), step.get("operation")):
                if val not in (None, ""):
                    pre_logs.append(val)
            init_subs.append(sub)
            steps_out.append({"no": step.get("no"), "header_log": False})
            continue
        steps_out.append(
            _convert_step(step, in_sigs, exp_sigs, action_sub=sub or None))

    doc: dict[str, Any] = {
        "test_case_id": row_test_id(test_row),
        "lib_json": lib_json_name,
        "default_timeout": body.get("default_timeout", DEFAULT_TIMEOUT),
    }
    if init_subs or pre_logs:
        doc["pre_init"] = {"logs": pre_logs, "init_subroutines": init_subs}
    elif isinstance(body.get("pre_init"), dict):
        doc["pre_init"] = body["pre_init"]
    doc["steps"] = steps_out
    return doc


# --------------------------------------------------------------------------- #
# Run-directory materialisation
# --------------------------------------------------------------------------- #
def materialise_run_dir(
    case_dir: Path,
    test_row: TestItemRow,
    const_rows: Iterable[TestItemRow],
    lib_rows: Iterable[TestItemRow],
) -> dict:
    """Assemble a self-contained JSON-runner directory at *case_dir*.

    Writes ``testcase_<id>.json`` + ``lib.json`` + ``constants.json`` and copies
    the runner framework files beside them. Returns a small manifest of the
    written paths (the runner path is what ``silver_runner`` looks for).
    """
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)

    runner_path = silver_json.copy_framework(case_dir)

    const_doc = build_constants(const_rows)
    lib_doc = build_lib(lib_rows)
    init_names = {
        name for name, entry in lib_doc.get("subroutines", {}).items()
        if isinstance(entry, dict) and entry.get("kind") == "init"
    }
    case_doc = build_testcase(
        test_row, init_names=init_names, lib_json_name="lib.json")

    tid = safe_test_id(row_test_id(test_row))
    testcase_name = f"testcase_{tid}.json"

    (case_dir / "constants.json").write_text(
        json.dumps(const_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "lib.json").write_text(
        json.dumps(lib_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / testcase_name).write_text(
        json.dumps(case_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "runner": runner_path,
        "testcase_json": case_dir / testcase_name,
        "constants_json": case_dir / "constants.json",
        "lib_json": case_dir / "lib.json",
    }
