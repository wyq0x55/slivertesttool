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
from framework_builtins import BUILTINS, MagicList
from step_parser import (
    _BIN_OPS, _UNARY_OPS, _OP_PREFIXES, _INTERVAL_RE, _INF_LO, _INF_HI,
    _LABEL_EXPR_RE, _const_value, _const_name_ja, _split_operator,
    _looks_like_interval, _parse_interval, _eval_node, _eval_expr,
    _resolve_single, _resolve_value, _param_names, _resolve_arg, build_steps,
)

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
        return {name: (build_steps(sub, consts), _param_names(sub))
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
        subs[name] = (build_steps(sub_spec, consts), _param_names(sub_spec))
    return subs


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

time = Variable('currentTime')
stepsize = Variable('modelStepSize')
digit = len(str(stepsize.Value).split('.')[1])

_ctx = TestContext(Variable, logging, time, digit, DLL_OK,
                   stepsize=stepsize, lib=_LIB)
for _sname, (_ssteps, _sparams) in _SUBS.items():
    _ctx.register_subroutine(_sname, _ssteps, _sparams)

_started = 'Test case ID.ID;;' + _SPEC.get('test_case_id', '') + ' is started!'
print(datetime.datetime.today())
print(_started)

def _bind_log_file(path):
    """Point the root logger at *path*, exclusively.

    ``logging.basicConfig`` is a no-op once the root logger owns a handler. A
    pooled Silver instance can keep one Python interpreter alive across runs, so
    basicConfig would silently keep writing this run's verdict into the
    *previous* run's jdgrslt.log: the earlier task got foreign lines appended and
    this task got an empty file, i.e. an UNKNOWN verdict. Closing the inherited
    handlers and opening our own guarantees the verdict lands in the file the
    caller asked for.

    Opening with mode 'w' truncates in place. The previous ``os.remove`` raced
    with the console tailer and, on Windows, raised outright when the file was
    still held open by a handler from a prior load.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - a stale handle must not abort the run
            pass
    handler = logging.FileHandler(path, mode='w', encoding='UTF-8')
    handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_bind_log_file(logfile)
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
        run_init_subroutine(_ctx, name, _SUBS[name][0])
    return DLL_OK


def MainGenerator(*args):
    # Safety net: a failure raised while running a lib / nested subroutine
    # (e.g. a judge-idiom generator that returned, or any test-logic error) must
    # be reported as a normal test FAIL, never propagated to Silver. An exception
    # escaping here is caught by Silver as a DLL_ERROR, which the runner
    # classifies as an environment ERROR and which then cancels the rest of the
    # project's queue -- a single flaky nested lib must not do that. Finish the
    # run cleanly with a FAIL verdict instead so only THIS test is marked failed.
    try:
        yield from run_test(_ctx, _STEPS)
    except Exception as exc:            # noqa: BLE001
        step_no = _ctx.test_step_no if _ctx.test_step_no != -1 else 1
        if _ctx.test_result == -1:
            _ctx.test_result = step_no
        logging.info('Test aborted by exception in lib: %r' % exc)
        print('Test is failed in Step%s!!!!' % _ctx.test_result)
        logging.info('Test is failed in Step%s!!!!' % _ctx.test_result)
        _ctx.test_python_over = 0
        for _ in range(10):
            yield


def pre_cleanup(time):
    return run_cleanup(_ctx, time)
