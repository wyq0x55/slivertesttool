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
LIB_PARA = "lib_para"

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
# An interval / range cell such as "[0,50)" or "(0,50]" — optionally carrying a
# 和名 prefix like "カウント開始[0,65535)" — is detected here so the paren-splitter
# above neither strips its brackets nor mistakes "(lo,hi)" for a single
# identifier. The bracketed part is returned as the identifier; the runner
# resolves any constant names inside it and honours [ ] (inclusive) vs ( )
# (exclusive) bounds. Half- and full-width brackets / separators are accepted.
_INTERVAL_OPEN = "\\[\\(\uff3b\uff08"          # [ ( （fw） ［fw］
_INTERVAL_CLOSE = "\\]\\)\uff3d\uff09"         # ] ) ）fw） ］fw］
_INTERVAL_SEP = ",\u3001\uff0c"                # , 、 ，
_INTERVAL_CELL_RE = re.compile(
    r"^(?P<jname>.*?)"
    r"(?P<interval>[" + _INTERVAL_OPEN + r"]"
    r"[^" + _INTERVAL_OPEN + _INTERVAL_CLOSE + r"]*?"
    r"[" + _INTERVAL_SEP + r"]"
    r"[^" + _INTERVAL_OPEN + _INTERVAL_CLOSE + r"]*?"
    r"[" + _INTERVAL_CLOSE + r"])\s*$",
    re.S)


def _blank_cell(cell: Any) -> bool:
    """A signal cell with no value: ``None``, empty, or the ``-`` placeholder."""
    if cell is None:
        return True
    return str(cell).strip() in ("", "-")


def _split_cell(cell: Any) -> tuple[str, str]:
    """Split a value cell into ``(jname, identifier)``.

    Three shapes are recognised, in priority order:

    * an interval / range such as ``[0,50)`` / ``(0,65535]``, optionally with a
      ``和名`` prefix (``カウント開始[U1G_DATA_ZERO,U2G_DAT_MAX)``). The bracketed
      part is returned verbatim as the identifier so the runner can resolve the
      constant names inside it and honour ``[`` ``]`` (inclusive) vs ``(`` ``)``
      (exclusive) bounds; the prefix becomes ``jname``.
    * ``和名(識別子)`` — a named constant / parameter; both full-width ``（）`` and
      half-width ``()`` parentheses are accepted.
    * anything else is treated as a bare identifier (``jname=''``).
    """
    s = "" if cell is None else str(cell).strip()
    m = _INTERVAL_CELL_RE.match(s)
    if m:
        return m.group("jname").strip(), m.group("interval").strip()
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


def _body_signal_paths(body: dict) -> list[str]:
    """The Silver variable paths of a step-body's input + expected signals.

    These are the ``path`` halves of ``input_signals`` / ``expected_signals``
    (the same identifiers each step uses as its ``var``), i.e. exactly the
    signals a case reads or checks — the set worth recording to ``output.csv``.
    Blank paths are dropped.
    """
    paths: list[str] = []
    for group in ("input_signals", "expected_signals"):
        for _name, path in _signal_pairs(body.get(group)):
            token = (path or "").strip()
            if token:
                paths.append(token)
    return paths


def build_signal_list(test_row: TestItemRow,
                      lib_rows: Iterable[TestItemRow]) -> list[str]:
    """Ordered, de-duplicated Silver signals relevant to one test case.

    Combines the test row's own input/expected signals with those of every lib
    subroutine (its ``lib_stb`` body), so CsvWriter's ``-l`` list records exactly
    the signals the case — and the subroutines it calls — reads or checks. Order
    is preserved (test row first, then libs in row order) and duplicates removed.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(paths: list[str]) -> None:
        for token in paths:
            if token not in seen:
                seen.add(token)
                ordered.append(token)

    _add(_body_signal_paths(_parse_json_field(test_row.get_field(TEST_STEPS))))
    for row in lib_rows:
        _add(_body_signal_paths(_parse_json_field(row.get_field(LIB_STEPS))))
    return ordered


def _step_subroutine(step: dict) -> str:
    sub = step.get("subroutine")
    return str(sub).strip() if sub not in (None, "") else ""


def _convert_step(step: dict,
                  in_sigs: list[tuple[str, str]],
                  exp_sigs: list[tuple[str, str]],
                  *, action_sub: Optional[str] = None,
                  params: Optional[dict] = None) -> dict:
    """Turn one columnar step into a runner ``steps[]`` entry.

    Key order matches the reference JSON: ``no, category, comment, actions,
    inputs, checks``. Empty sections are omitted entirely.

    *params* (lib subroutines only) maps a subroutine's formal-parameter names
    (``lib_para``) to their declared default. A value / expected cell naming a
    parameter is emitted with the default as its baked value plus a ``param``
    marker; at run time the caller's matching argument (実引数, emitted on the
    calling step's ``args``) overrides that default via the ``param`` marker.
    """
    out: dict[str, Any] = {"no": step.get("no")}

    purpose = step.get("purpose")
    if purpose not in (None, ""):
        out["category"] = purpose
    operation = step.get("operation")
    if operation not in (None, ""):
        out["comment"] = operation

    if action_sub:
        action: dict[str, Any] = {"subroutine": action_sub}
        call_args = _parse_call_args(step.get("args"))
        if call_args:
            action["args"] = call_args
        out["actions"] = [action]

    inputs: list[dict] = []
    in_cells = step.get("inputs") or []
    for j, (_name, path) in enumerate(in_sigs):
        cell = in_cells[j] if j < len(in_cells) else None
        if _blank_cell(cell):
            continue
        _jname, ident = _split_cell(cell)
        if params is not None and ident in params:
            # The cell names a formal parameter (lib_para), not a constant. Emit
            # the parameter's declared default as the baked value (0 when none)
            # and tag it with ``param``; the runner overrides this with the
            # caller's actual argument (実引数) for that parameter at run time.
            dflt = params[ident]
            inputs.append({"var": path, "value": 0 if dflt is None else dflt,
                           "param": ident})
        else:
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
        if params is not None and ident in params:
            dflt = params[ident]
            chk: dict[str, Any] = {"var": path, "label": name,
                                   "expected": 0 if dflt is None else dflt,
                                   "param": ident}
        else:
            chk = {"var": path, "label": name, "expected": ident}
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


# A ``lib_para`` (仮引数) token separator: comma / full-width comma / ideographic
# comma / any newline. The field is authored as a single- or multi-value list.
_LIB_PARA_SEP_RE = re.compile(r"[,\uFF0C\u3001\n\r]+")


def _parse_call_args(raw: Any) -> list[str]:
    """Split a step's ``args`` cell (実引数) into ordered positional tokens.

    Uses the same comma / full-width comma / ideographic comma / newline
    separators as a formal-parameter list. Each token is emitted verbatim (a
    constant name, numeric / hex literal, or arithmetic expression); the runner
    resolves it against the constant table when the subroutine is invoked. A
    blank or ``-`` placeholder cell yields no arguments.
    """
    if raw is None:
        return []
    text = raw if isinstance(raw, str) else str(raw)
    if text.strip() in ("", "-"):
        return []
    return [tok.strip() for tok in _LIB_PARA_SEP_RE.split(text) if tok.strip()]


def _parse_lib_params(raw: Any) -> list[tuple[str, Any]]:
    """Parse a ``lib_para`` field into ordered ``(name, default)`` pairs.

    Formal parameters are authored as a comma/newline-separated list; each entry
    is a bare name (``value``) or ``name=default`` (``value1=0``). A single
    parameter (``value``) and multiple parameters (``value1,value2``) are both
    accepted, with optional defaults (``value1=0,value2=1``). Defaults are
    coerced numerically where possible; a bare name has a ``None`` default.
    Duplicate names keep their first occurrence.
    """
    if raw is None:
        return []
    text = raw if isinstance(raw, str) else str(raw)
    out: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for tok in _LIB_PARA_SEP_RE.split(text):
        tok = tok.strip()
        if not tok:
            continue
        if "=" in tok:
            name, _, dflt = tok.partition("=")
            name = name.strip()
            default: Any = _coerce_const_value(dflt.strip())
        else:
            name, default = tok, None
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((name, default))
    return out


def _lib_rows_by_name(lib_rows: Iterable[TestItemRow]) -> dict[str, TestItemRow]:
    """Index lib rows by their subroutine name (``lib_func`` / ``lib_name`` / id)."""
    by_name: dict[str, TestItemRow] = {}
    for row in lib_rows:
        name = (row.get_field(LIB_FUNC) or row.get_field(LIB_NAME)
                or row.case_id or "")
        name = str(name).strip()
        if name and name not in by_name:
            by_name[name] = row
    return by_name


def _body_subroutines(body: dict) -> list[str]:
    """The non-empty ``subroutine`` names referenced by a step-doc body."""
    names: list[str] = []
    for step in _steps_list(body):
        if not isinstance(step, dict):
            continue
        sub = _step_subroutine(step)
        if sub:
            names.append(sub)
    return names


def collect_used_subroutines(test_row: TestItemRow,
                             lib_rows: Iterable[TestItemRow]) -> set[str]:
    """Names of every subroutine the test case reaches, transitively.

    Starts from the ``subroutine`` actions of the test row's own steps and
    follows each referenced lib's steps (a subroutine may call further
    subroutines), so pruning ``lib.json`` to this set never drops a procedure the
    case actually calls. Unknown names (referenced but absent from the lib sheet)
    are ignored — ``build_lib`` simply omits them.
    """
    by_name = _lib_rows_by_name(lib_rows)
    used: set[str] = set()
    stack = _body_subroutines(_parse_json_field(test_row.get_field(TEST_STEPS)))
    while stack:
        name = stack.pop()
        if name in used:
            continue
        used.add(name)
        row = by_name.get(name)
        if row is not None:
            stack.extend(
                _body_subroutines(_parse_json_field(row.get_field(LIB_STEPS))))
    return used


def build_lib(lib_rows: Iterable[TestItemRow], *,
              used_names: Optional[Iterable[str]] = None) -> dict:
    """``lib`` sheet rows -> ``lib.json`` document.

    Each row's ``lib_stb`` field carries the same columnar 手順 block as a test
    case; ``isinit`` selects the ``init`` vs ``process`` kind. The subroutine
    name is the row's ``lib_func`` (fallback ``lib_name`` / ``case_id``). The
    columnar steps are converted to the runner's per-step schema so a
    subroutine's ``inputs`` / ``checks`` resolve exactly like a test case's.

    When *used_names* is given, only subroutines whose name appears in it are
    emitted (see :func:`collect_used_subroutines`), so ``lib.json`` carries just
    the procedures the test case actually calls instead of the whole library.

    A row's ``lib_para`` (仮引数) declares formal parameters with optional
    defaults; each is recorded on the subroutine entry as ``params`` and any
    value / expected cell naming a parameter is bound to its default rather than
    resolved as a constant.
    """
    want = (None if used_names is None
            else {str(n).strip() for n in used_names if str(n).strip()})
    subs: dict[str, dict] = {}
    for row in lib_rows:
        name = (row.get_field(LIB_FUNC) or row.get_field(LIB_NAME)
                or row.case_id or "")
        name = str(name).strip()
        if not name:
            continue
        if want is not None and name not in want:
            continue
        body = _parse_json_field(row.get_field(LIB_STEPS))
        in_sigs = _signal_pairs(body.get("input_signals"))
        exp_sigs = _signal_pairs(body.get("expected_signals"))
        params = _parse_lib_params(row.get_field(LIB_PARA))
        param_map = {n: d for n, d in params}
        steps_out: list[dict] = []
        for step in _steps_list(body):
            if not isinstance(step, dict):
                continue
            sub = _step_subroutine(step)
            steps_out.append(
                _convert_step(step, in_sigs, exp_sigs, action_sub=sub or None,
                              params=param_map))
        entry: dict[str, Any] = {
            "kind": "init" if _as_bool(row.get_field(LIB_ISINIT)) else "process",
        }
        if params:
            entry["params"] = [
                ({"name": n, "default": d} if d is not None else {"name": n})
                for n, d in params]
        entry["steps"] = steps_out
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

    # Materialise lib_rows once: it is consumed twice below (build_lib + the
    # CsvWriter signal list), so a bare generator would come up empty the second
    # time.
    lib_rows = list(lib_rows)

    runner_path = silver_json.copy_framework(case_dir)

    const_doc = build_constants(const_rows)
    # Emit only the subroutines this test case actually reaches (transitively),
    # so lib.json is scoped to the procedure instead of the whole library.
    used_names = collect_used_subroutines(test_row, lib_rows)
    lib_doc = build_lib(lib_rows, used_names=used_names)
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

    # CsvWriter signal-selection list (``output.txt``). One Silver signal name
    # per line, no comments. When present, ``silver_runner`` passes it as
    # CsvWriter's ``-l`` argument so ``output.csv`` records only the case-relevant
    # signals (test row + every lib subroutine it may call); when the case
    # declares no signals we skip the file entirely, and CsvWriter falls back to
    # recording all variables.
    output_txt = case_dir / "output.txt"
    # Scope the signal list to the same subroutines emitted into lib.json.
    used_lib_rows = [
        row for row in lib_rows
        if str(row.get_field(LIB_FUNC) or row.get_field(LIB_NAME)
                or row.case_id or "").strip() in used_names
    ]
    signal_paths = build_signal_list(test_row, used_lib_rows)
    if signal_paths:
        output_txt.write_text("\n".join(signal_paths) + "\n", encoding="utf-8")

    return {
        "runner": runner_path,
        "testcase_json": case_dir / testcase_name,
        "constants_json": case_dir / "constants.json",
        "lib_json": case_dir / "lib.json",
        "output_txt": output_txt if signal_paths else None,
    }
