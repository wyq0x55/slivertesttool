# coding: UTF-8
"""
Unified, data-driven Silver test runner.

ONE framework file runs ANY test case: the test steps live entirely in an
external UTF-8 JSON file, so creating a new test == writing a new JSON.

Output is byte-for-byte compatible with the legacy judge-*.py scripts
(console print + logging file, ●/▲ markers, Monitoring/Expected/Observed
blocks, per-step pass/fail, timeout, pre_init / pre_cleanup suspension).

How Silver calls it
-------------------
    argv[1] = log file path            (as in the legacy judge scripts)
    argv[2] = test-case JSON path       (optional)

If argv[2] is omitted, the JSON path is taken from the environment variable
SILVER_TEST_JSON, otherwise the single *.json file sitting next to this
script is used.

JSON schema (see testcase_*.json for a full example)
----------------------------------------------------
{
  "test_case_id": "MWCPD-...-TC_APL-001004",
  "const_modules": ["Common_Constant", "Constant", "Bit", "Extend", "Wait"],
  "pre_init": { "logs": ["...", "..."], "system_initialize": true },
  "default_timeout": 5,
  "steps": [
    { "no": 1, "header_log": false },
    { "no": 2, "category": "前提条件の確認", "comment": "...",
      "checks": [
        { "var": "...", "label": "CPD Mod",
          "expected": "U1G_...", "desc": "...", "timing": "任意" }
      ] },
    { "no": 3, "category": "トリガ入力", "comment": "...",
      "inputs": [ { "var": "...", "value": "SIGVAL_DOOR_OPEN" } ],
      "checks": [ ... ] }
  ]
}

Rules for values
----------------
* input "value" and check "expected":
    - number  -> used literally
    - string  -> evaluated as an expression over the constant table:
        * a bare constant NAME resolves to its value (as before)
        * hex literals (0x12), decimals and the arithmetic operators
          ``+ - * / % ** // & | ^ << >>`` are supported, e.g. "BASE + 0x4"
* check "expected" may additionally start with a relational operator so the
  check accepts a range/threshold instead of a single value:
      "> 5", ">= 0x10", "!= ERR_NONE", "<= THRESHOLD"
  Full-width / mathematical operator glyphs are accepted too, e.g.
      "≧THRESHOLD" (>=), "≦MAX" (<=), "≠ERR" (!=), "＞5" (>), "＜10" (<).
* check "expected" may also be an interval so the check accepts a numeric range:
      "[0, 50)"  -> 0 <= observed <  50   (0~49)
      "[0, 50]"  -> 0 <= observed <= 50   (0~50)
      "(0, 50]"  -> 0 <  observed <= 50
  Bounds may be constants/expressions and either side may be left empty for an
  unbounded end, e.g. "[0, )". Full-width brackets/commas/digits are accepted.
  The default (no operator) keeps equality semantics and prints
  "(exp==<NAME>(<value>))" exactly like judge.
"""

import sys, os, json, importlib, logging, datetime, ast, operator as _operator, re
import unicodedata

try:
    from synopsys.silver import *
    from synopsys.util import scheduler
except ImportError:
    from qtronic.silver import *
    from qtronic.util import scheduler

# When Silver loads this runner by absolute path (Python.dll <path>), the
# runner's own directory is not guaranteed to be on sys.path, so the sibling
# framework modules below would fail to import. Add it explicitly.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from silver_test_framework import (
    Assign, Call, SubCall, Check, Step, TestContext,
    run_test, run_cleanup, run_init_subroutine,
)
from framework_builtins import BUILTINS

DEFAULT_CONST_MODULES = ['Common_Constant', 'Constant', 'Bit', 'Extend', 'Wait']


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def _peek_json_kind(path):
    """Classify a JSON file by its content, so CLI args can be given in any
    order:  test-case / constants / lib.  Returns one of
    'test' | 'constants' | 'lib' | None."""
    try:
        with open(path, encoding='UTF-8') as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if 'subroutines' in data:
        return 'lib'
    if 'constants' in data:
        return 'constants'
    if 'steps' in data or 'test_case_id' in data:
        return 'test'
    return None


def _parse_cli(script_dir):
    """
    Flexible argument parsing.  Any *.json argument is classified by content
    (test / constants / lib) regardless of position; the first non-JSON
    argument is taken as the log file path.  This supports both:
      * Silver:  silver_json_runner <logfile> <testcase.json>
      * manual:  silver_json_runner testcase.json constants.json lib.json
    Missing pieces fall back to env / spec fields / directory scan later.
    """
    cli = {'logfile': None, 'test': None, 'constants': None, 'lib': None}
    for arg in sys.argv[1:]:
        if not arg:
            continue
        if arg.lower().endswith('.json') and os.path.isfile(arg):
            kind = _peek_json_kind(arg)
            if kind == 'lib':
                cli['lib'] = arg
            elif kind == 'constants':
                cli['constants'] = arg
            elif kind == 'test' or cli['test'] is None:
                cli['test'] = arg
        elif cli['logfile'] is None:
            cli['logfile'] = arg
    return cli


def _resolve_json_path(cli, script_dir):
    if cli['test']:
        return cli['test']
    env = os.environ.get('SILVER_TEST_JSON')
    if env:
        return env
    candidates = [f for f in os.listdir(script_dir)
                  if f.lower().endswith('.json') and _peek_json_kind(
                      os.path.join(script_dir, f)) == 'test']
    if len(candidates) == 1:
        return os.path.join(script_dir, candidates[0])
    raise RuntimeError(
        'Cannot locate test JSON. Pass it on the command line, set '
        'SILVER_TEST_JSON, or keep exactly one test *.json next to the runner. '
        'Found: %r' % candidates)


def _extend_lib_sys_path(script_dir):
    """Same Lib search paths the legacy judge scripts use."""
    lib_in = os.path.normpath(os.path.join(script_dir, "../../Lib/"))
    lib_co = os.path.normpath(os.path.join(
        script_dir.split('01_Spec_Report')[0], "./02_Config/Library/Lib/"))
    lib_std = os.path.normpath(os.path.join(
        script_dir.split('01_Spec_Report')[0], "./02_Config/Library/StdLib/"))
    for p in (lib_in, lib_co, lib_std):
        if p not in sys.path:
            sys.path.append(p)


# --------------------------------------------------------------------------- #
# Constant resolution
# --------------------------------------------------------------------------- #
def _load_constants_from_json(path):
    """Load constants from a shared JSON  { "NAME": value, ... }."""
    with open(path, encoding='UTF-8') as f:
        data = json.load(f)
    # allow either a flat map or {"constants": {...}}
    return data.get('constants', data) if isinstance(data, dict) else {}


def _load_constants_from_modules(module_names):
    consts = {}
    for name in module_names:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        for k, v in vars(mod).items():
            if not k.startswith('_'):
                consts.setdefault(k, v)
    return consts


def _load_constants(spec, script_dir, cli_const=None):
    """
    Priority:
      CLI constants.json  >  spec 'constants_json'  >  a 'constants.json'
      sitting next to the runner  >  import the generated *.py constant
      modules (backward compatibility).
    The test JSON therefore no longer needs a 'constants_json' field.
    """
    ref = cli_const or spec.get('constants_json')
    if not ref:
        default = os.path.join(script_dir, 'constants.json')
        if os.path.isfile(default):
            ref = default
    if ref:
        path = ref if os.path.isabs(ref) else os.path.join(script_dir, ref)
        return _load_constants_from_json(path)
    return _load_constants_from_modules(
        spec.get('const_modules', DEFAULT_CONST_MODULES))


def _const_value(entry):
    """A constants.json entry is either a bare value or a rich
    { "value": v, "name_ja": ..., "remark": ... } object."""
    if isinstance(entry, dict) and 'value' in entry:
        return entry['value']
    return entry


def _const_name_ja(consts, name):
    """The 和名 of a constant (used as the default check `desc`)."""
    entry = consts.get(name)
    if isinstance(entry, dict):
        return entry.get('name_ja', '')
    return ''


# --------------------------------------------------------------------------- #
# Safe expression evaluation for value cells
#
# A value/expected cell may be a plain constant name, a numeric literal
# (decimal or 0x-hex), or an arithmetic expression combining them, e.g.
# "BASE + 0x4" or "MAX * 2".  A check's expected may also carry a leading
# relational operator ("> 5", ">= 0x10").  Expressions are parsed with ``ast``
# and evaluated over a fixed operator whitelist -- no ``eval`` / arbitrary code.
# --------------------------------------------------------------------------- #
_BIN_OPS = {
    ast.Add: _operator.add, ast.Sub: _operator.sub, ast.Mult: _operator.mul,
    ast.Div: _operator.truediv, ast.Mod: _operator.mod, ast.Pow: _operator.pow,
    ast.FloorDiv: _operator.floordiv,
    ast.BitAnd: _operator.and_, ast.BitOr: _operator.or_,
    ast.BitXor: _operator.xor, ast.LShift: _operator.lshift,
    ast.RShift: _operator.rshift,
}
_UNARY_OPS = {ast.UAdd: _operator.pos, ast.USub: _operator.neg,
              ast.Invert: _operator.invert}

# Leading relational operators, longest / compound first so ">=" wins over ">".
# Each canonical ASCII op lists the glyphs (ASCII + full-width + mathematical)
# that may introduce it in a cell.
_OP_PREFIXES = (
    ('>=', ('>=', '=>', '\u2265', '\u2267', '\uff1e\uff1d', '\uff1e=')),  # ≥ ≧ ＞＝
    ('<=', ('<=', '=<', '\u2264', '\u2266', '\uff1c\uff1d', '\uff1c=')),  # ≤ ≦ ＜＝
    ('!=', ('!=', '\u2260', '<>', '\uff1c\uff1e', '\uff01\uff1d', '\uff01=')),  # ≠ ＜＞ ！＝
    ('==', ('==', '\uff1d\uff1d', '=', '\uff1d')),                        # ＝＝ ＝
    ('>',  ('>', '\uff1e')),                                              # ＞
    ('<',  ('<', '\uff1c')),                                              # ＜
)


def _split_operator(text):
    """Split a leading relational operator off an expected cell.

    Returns ``(op, rest)``; when no operator is present the default ``'=='`` is
    returned with the whole (stripped) text. Full-width and mathematical
    operator glyphs (≧ ≦ ≠ ＞ ＜ ＝ …) are recognised as their ASCII form.
    """
    s = text.strip()
    for canon, prefixes in _OP_PREFIXES:
        for p in prefixes:
            if s.startswith(p):
                return canon, s[len(p):].strip()
    return '==', s


# An interval / range cell: "[0, 50)", "(0, 50]", "[MIN, MAX)", "[0, )" ...
# A bound may be empty (unbounded) or an expression over the constant table.
# The separator may be an ASCII / full-width / ideographic comma.
_INTERVAL_RE = re.compile(
    r'^([\[\(])\s*(.*?)\s*[,\u3001\uff0c]\s*(.*?)\s*([\]\)])$', re.S)
_INF_LO = ('', '-inf', '-\u221e', '\u221e-')            # empty, -inf, -∞
_INF_HI = ('', 'inf', '+inf', '\u221e', '+\u221e')      # empty, inf, +∞


def _looks_like_interval(text):
    """True when ``text`` (as-is) is bracketed like an interval cell."""
    s = unicodedata.normalize('NFKC', str(text).strip())
    return bool(_INTERVAL_RE.match(s))


def _parse_interval(text, consts):
    """Parse an interval cell -> ``(lo, hi, lo_incl, hi_incl)``.

    ``[`` / ``]`` are inclusive bounds, ``(`` / ``)`` exclusive. An empty or
    ``±inf`` bound means unbounded on that side (``None``). Bounds are evaluated
    over the constant table. Returns ``None`` when ``text`` is not an interval.
    """
    s = unicodedata.normalize('NFKC', str(text).strip())
    m = _INTERVAL_RE.match(s)
    if not m:
        return None
    lb, lo_s, hi_s, rb = m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4)
    lo = None if lo_s.lower() in _INF_LO else _eval_expr(lo_s, consts)
    hi = None if hi_s.lower() in _INF_HI else _eval_expr(hi_s, consts)
    return (lo, hi, lb == '[', rb == ']')


def _eval_node(node, consts):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, consts)
    # Numeric / string literal (ast.Num on <3.8, ast.Constant on >=3.8).
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Num):  # pragma: no cover - legacy Python
        return node.n
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left, consts),
                                       _eval_node(node.right, consts))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand, consts))
    if isinstance(node, ast.Name):
        if node.id in consts:
            return _const_value(consts[node.id])
        raise KeyError('Unknown constant name in JSON: %r' % node.id)
    raise ValueError('Unsupported expression element: %r'
                     % ast.dump(node))


def _eval_expr(text, consts):
    """Evaluate a constant-table expression (hex/decimal/arithmetic/names)."""
    tree = ast.parse(text, mode='eval')
    return _eval_node(tree, consts)


def _resolve_value(raw, consts):
    """Resolve a value cell.

    number -> literal; string -> a constant NAME, a numeric/hex literal, or an
    arithmetic expression over the constant table.
    """
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    # Fast path + backward compatibility: an exact constant name (which may not
    # be a valid Python identifier) resolves directly.
    if s in consts:
        return _const_value(consts[s])
    try:
        return _eval_expr(s, consts)
    except KeyError:
        raise
    except Exception:
        raise KeyError('Unknown constant name in JSON: %r' % raw)


def _load_lib(module_names):
    """
    Start with the embedded framework builtins (Wait / bit /
    Extend_CompareNvramWith / MagicList), then import any extra project lib
    modules and collect their callables by name.
    """
    lib = dict(BUILTINS)
    for name in module_names:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        for k, v in vars(mod).items():
            if not k.startswith('_') and callable(v):
                lib.setdefault(k, v)
    return lib


def _load_subroutines(spec, consts, script_dir, cli_lib=None):
    """
    Return { name: List[Step] } for every generated subroutine (init + process).

    Priority:
      * a single bundled lib JSON  { "subroutines": { name: {steps..}, .. } }
        given on the CLI or via the spec's 'lib_json' field (recommended);
      * else individual <name>.json files next to the runner (backward compat).
    A subroutine entry uses the same step schema as a test case.
    """
    lib_ref = cli_lib or spec.get('lib_json')
    if lib_ref:
        path = lib_ref if os.path.isabs(lib_ref) else os.path.join(script_dir, lib_ref)
        with open(path, encoding='UTF-8') as f:
            bundle = json.load(f)
        return {name: build_steps(sub, consts)
                for name, sub in bundle.get('subroutines', {}).items()}

    names = set()
    for s in spec['steps']:
        for a in s.get('actions', []):
            if 'subroutine' in a:
                names.add(a['subroutine'])
    for name in spec.get('pre_init', {}).get('init_subroutines', []):
        names.add(name)
    subs = {}
    for name in names:
        path = os.path.join(script_dir, name + '.json')
        with open(path, encoding='UTF-8') as f:
            sub_spec = json.load(f)
        subs[name] = build_steps(sub_spec, consts)
    return subs


def _resolve_arg(raw, consts):
    """Call args: strings that name a constant resolve to it, else literal."""
    if isinstance(raw, str) and raw in consts:
        return _const_value(consts[raw])
    return raw


# --------------------------------------------------------------------------- #
# JSON -> Step objects
# --------------------------------------------------------------------------- #
def build_steps(spec, consts):
    default_timeout = spec.get('default_timeout', 5)
    steps = []
    for s in spec['steps']:
        inputs = [Assign(a['var'], _resolve_value(a['value'], consts))
                  for a in s.get('inputs', [])]
        actions = []
        for a in s.get('actions', []):
            if 'subroutine' in a:
                actions.append(SubCall(name=a['subroutine']))
            else:
                actions.append(Call(
                    func=a['call'],
                    args=[_resolve_arg(x, consts) for x in a.get('args', [])],
                    kind=a.get('kind', 'auto'),
                    store=a.get('store'),
                ))
        checks = []
        for c in s.get('checks', []):
            exp_raw = c['expected']
            # An expected string may be an interval ("[0, 50)"), carry a leading
            # relational operator ("≧THRESH"), and/or be an arithmetic/hex
            # expression; a non-string is used literally.
            if isinstance(exp_raw, str) and _looks_like_interval(exp_raw):
                op = 'in'
                exp_name = exp_raw.strip()
                exp_value = _parse_interval(exp_raw, consts)
            elif isinstance(exp_raw, str):
                op, expr = _split_operator(exp_raw)
                exp_name = expr
                exp_value = _resolve_value(expr, consts)
            else:
                op, exp_name = '==', str(exp_raw)
                exp_value = _resolve_value(exp_raw, consts)
            # `desc` defaults to the expected constant's 和名 (name_ja) when the
            # expected is a bare constant name; an explicit `desc` overrides it.
            exp_desc = c.get('desc')
            if exp_desc is None:
                exp_desc = _const_name_ja(consts, exp_name)
            checks.append(Check(
                var=c['var'],
                label=c['label'],
                exp_name=exp_name,
                exp_value=exp_value,
                exp_desc=exp_desc,
                timing=c.get('timing', '任意'),
                op=op,
            ))
        steps.append(Step(
            no=s['no'],
            header_log=s.get('header_log', True),
            category=s.get('category'),
            comment=s.get('comment'),
            inputs=inputs,
            actions=actions,
            checks=checks,
            timeout=s.get('timeout', default_timeout),
            method=s.get('method', 'reach'),
            watch_ms=s.get('watch_ms', 0),
        ))
    return steps


# --------------------------------------------------------------------------- #
# Wire-up (module level, like the legacy judge scripts)
# --------------------------------------------------------------------------- #
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_extend_lib_sys_path(_SCRIPT_DIR)

_CLI = _parse_cli(_SCRIPT_DIR)
_json_path = _resolve_json_path(_CLI, _SCRIPT_DIR)

with open(_json_path, encoding='UTF-8') as _f:
    _SPEC = json.load(_f)

_CONSTS = _load_constants(_SPEC, _SCRIPT_DIR, cli_const=_CLI['constants'])
_LIB = _load_lib(_SPEC.get('lib_modules', []))
_SUBS = _load_subroutines(_SPEC, _CONSTS, _SCRIPT_DIR, cli_lib=_CLI['lib'])
_STEPS = build_steps(_SPEC, _CONSTS)

logfile = (_CLI['logfile']
           or os.environ.get('SILVER_LOG')
           or os.path.join(_SCRIPT_DIR,
                           (_SPEC.get('test_case_id') or 'judge') + '.log'))

if os.path.exists(logfile):
    os.remove(logfile)

time = Variable('currentTime')
stepsize = Variable('modelStepSize')
digit = len(str(stepsize.Value).split('.')[1])

_ctx = TestContext(Variable, logging, time, digit, DLL_OK,
                   stepsize=stepsize, lib=_LIB)
for _sname, _ssteps in _SUBS.items():
    _ctx.register_subroutine(_sname, _ssteps)

_started = 'Test case ID.ID;;' + _SPEC.get('test_case_id', '') + ' is started!'
print(datetime.datetime.today())
print(_started)

logging.basicConfig(filename=logfile, encoding='UTF-8', level=logging.INFO)
logging.info(datetime.datetime.today())
logging.info(_started)


# --------------------------------------------------------------------------- #
# Silver entry points
# --------------------------------------------------------------------------- #
def pre_init(time):
    logging.info('-------------------pre_init-------------------')
    _pre = _SPEC.get('pre_init', {})
    for line in _pre.get('logs', []):
        logging.info(line)
    for name in _pre.get('init_subroutines', []):
        run_init_subroutine(_ctx, name, _SUBS[name])
    return DLL_OK


def MainGenerator(*args):
    yield from run_test(_ctx, _STEPS)


def pre_cleanup(time):
    return run_cleanup(_ctx, time)
