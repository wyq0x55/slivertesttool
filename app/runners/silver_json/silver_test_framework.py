# coding: UTF-8
"""
Data-driven Silver test framework.

Idea (borrowed from python_runner.py):
    Describe a test case as *data* (a list of Step objects, each holding
    input assignments and判定 items) and let a single generic engine
    execute it.

Constraint (required by judge-*.py):
    Keep the FULL judge-style output byte-for-byte compatible:
      - console  : print(...)
      - log file : logging.info(...)  with ●/▲ markers,
                   "Monitoring target / Expected Value / Observed Value /
                    確認タイミング" blocks, per-step "Step.N is passed/failed",
                   timeout handling and pre_cleanup suspension output.

This module only provides the reusable engine.  A concrete test case
(see judge_data_driven.py) supplies the Step list and calls run_test().
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Assign:
    """One input assignment:  Variable(var).Value = value"""
    var: str
    value: Any


@dataclass
class Check:
    """
    One 判定 item.  Reproduces exactly the judge detail block:

        print(' <var> = ' + str(observed))
        logging.info('●' | '▲')
        logging.info('    Monitoring target: <label> ( <var> ) ')
        logging.info('    Expected Value:<desc> ( (exp==<exp_name>(<exp_value>)) ) ')
        logging.info('    Observed Value:<hex> ( <observed> ) ')
        logging.info('    確認タイミング：<timing>')
    """
    var: str            # Silver variable name (also printed on console line)
    label: str          # Monitoring target label
    exp_name: str       # expected constant NAME / expression, e.g. 'U1G_IG_OFF'
    exp_value: Any      # expected constant VALUE (already evaluated)
    exp_desc: str       # expected value human description
    timing: str = '任意'
    op: str = '=='      # comparison operator: == != > < >= <=


@dataclass
class Call:
    """
    A call into a project lib function, referenced by name.  The framework
    resolves `func` from the imported lib modules and dispatches by *shape*:

      kind == 'generator' : judge idiom  ->  gen = func(*args)
                                              while next(gen): yield
            (covers Wait(ms), SetTrigger_*_CmdOpr(), any co-routine subroutine)

      kind == 'function'  : result = func(*args)
            optionally stored in ctx.results[store] for a later Check.

      kind == 'auto'      : decided at run time via inspect.isgeneratorfunction.

    The lib module stays UNCHANGED; its own print/logging output is emitted
    verbatim, so judge-compatible output is preserved for free.
    """
    func: str
    args: List[Any] = field(default_factory=list)
    kind: str = 'auto'
    store: Optional[str] = None      # for kind='function': keep return value


@dataclass
class SubCall:
    """
    Invoke another *generated* test fragment that is itself defined in JSON
    (e.g. SetTrigger_CPD_STATE_SEARCH1_CmdOpr).  The framework runs it with the
    subroutine output flavor and drives it with the judge idiom, so it plugs in
    exactly where the legacy code did `gen = SubRoutine(); while next(gen): yield`.
    """
    name: str


@dataclass
class Step:
    no: int
    header_log: bool = True          # Step1 in judge prints header but no log
    category: Optional[str] = None   # 前提条件の確認 / トリガ入力 / 結果確認
    comment: Optional[str] = None    # StepN <comment>
    inputs: List[Assign] = field(default_factory=list)
    actions: List[Call] = field(default_factory=list)  # lib calls after inputs
    checks: List[Check] = field(default_factory=list)
    timeout: float = 5.0             # seconds (judge uses < 5)
    method: str = 'reach'            # 'reach' (wait until ok) | 'watch' (hold)
    watch_ms: float = 0.0            # duration for method='watch'


# --------------------------------------------------------------------------- #
# Engine context
# --------------------------------------------------------------------------- #
class TestContext:
    """
    Holds everything the engine needs.  `Variable` and `logging` are injected
    so the engine can be unit-tested with mocks (no Silver required).
    """

    def __init__(self, Variable, logging, time, digit, dll_ok,
                 stepsize=None, lib=None):
        self._Variable = Variable
        self._log = logging
        self._time = time
        self._digit = digit
        self._DLL_OK = dll_ok
        self._stepsize = stepsize          # Variable('modelStepSize'), for watch
        self._lib = lib or {}              # name -> callable (project lib)
        self._subs = {}                    # name -> List[Step] (JSON subroutines)
        self._cache = {}
        self.results = {}                  # captured lib return values

        # runtime state (mirror judge globals)
        self.test_python_over = -1
        self.test_step_no = -1
        self.test_result = -1
        self.current_step: Optional[Step] = None

    def stepsize(self):
        return self._stepsize.Value if self._stepsize is not None else 0.0

    def resolve_func(self, name):
        fn = self._lib.get(name)
        if fn is None:
            raise KeyError('Unknown lib function referenced in test: %r' % name)
        return fn

    def register_subroutine(self, name, steps):
        self._subs[name] = steps

    def resolve_subroutine(self, name):
        steps = self._subs.get(name)
        if steps is None:
            raise KeyError('Unknown subroutine referenced in test: %r' % name)
        return steps

    # -- variable access with caching (judge creates them once) ------------- #
    def var(self, name):
        v = self._cache.get(name)
        if v is None:
            v = self._Variable(name)
            self._cache[name] = v
        return v

    def now(self):
        return round(self._time.Value, self._digit)


# --------------------------------------------------------------------------- #
# Comparison  (a check's expected value may carry a relational operator)
# --------------------------------------------------------------------------- #
def _compare(observed: Any, op: str, expected: Any) -> bool:
    """Evaluate ``observed <op> expected``.

    ``op == '=='`` keeps the historical equality semantics (so ``MagicList``
    multi-value expecteds still work). The relational operators (> < >= <= !=)
    let a check accept a range/threshold instead of a single value.

    ``op == 'in'`` is an interval check whose ``expected`` is a 4-tuple
    ``(lo, hi, lo_incl, hi_incl)`` (either bound may be ``None`` = unbounded),
    e.g. ``[0, 50)`` -> ``(0, 50, True, False)`` (``0 <= observed < 50``).
    """
    if op == '!=':
        return observed != expected
    if op == '>':
        return observed > expected
    if op == '<':
        return observed < expected
    if op == '>=':
        return observed >= expected
    if op == '<=':
        return observed <= expected
    if op == 'in':
        lo, hi, lo_incl, hi_incl = expected
        if lo is not None and not (observed >= lo if lo_incl else observed > lo):
            return False
        if hi is not None and not (observed <= hi if hi_incl else observed < hi):
            return False
        return True
    return observed == expected


# --------------------------------------------------------------------------- #
# Output helpers  (single source of truth for the judge format)
# --------------------------------------------------------------------------- #
def _emit_check_detail(ctx: TestContext, chk: Check) -> bool:
    """Print + log one判定 item exactly like judge.  Returns pass/fail."""
    log = ctx._log
    observed = ctx.var(chk.var).Value
    ok = _compare(observed, chk.op, chk.exp_value)

    print(' ' + chk.var + ' = ' + str(observed))
    log.info('●' if ok else '▲')
    log.info('    Monitoring target: ' + chk.label + ' ( ' + chk.var + ' ) ')
    if chk.op == 'in':
        exp_detail = ' ( exp in ' + chk.exp_name + ' ) '
    else:
        exp_detail = (' ( (exp' + chk.op + chk.exp_name +
                      '(' + str(chk.exp_value) + ')) ) ')
    log.info('    Expected Value:' + chk.exp_desc + exp_detail)
    log.info('    Observed Value:' + str(hex(int(observed))) +
             ' ( ' + str(observed) + ' ) ')
    log.info('    確認タイミング：' + chk.timing)
    return ok


def _emit_step_detail(ctx: TestContext, step: Step):
    """Emit the detail block for every check of a step (used by cleanup too)."""
    for chk in step.checks:
        _emit_check_detail(ctx, chk)


def _run_action(ctx: TestContext, call: Call):
    """
    Execute one lib call.  Yields to Silver while a generator lib is running
    (judge idiom).  For a plain function, calls it and optionally stores the
    result.  The lib's own print/logging output is emitted verbatim.
    """
    import inspect
    fn = ctx.resolve_func(call.func)

    kind = call.kind
    if kind == 'auto':
        kind = 'generator' if inspect.isgeneratorfunction(fn) else 'function'

    if kind == 'generator':
        gen = fn(*call.args)          # exactly judge's:  gen = SubRoutine()
        while next(gen):              #                   while next(gen):
            yield                     #                       yield
    else:
        result = fn(*call.args)
        if call.store:
            ctx.results[call.store] = result


def _run_subcall(ctx: TestContext, sub: SubCall):
    """Drive a JSON-defined subroutine with the judge idiom."""
    steps = ctx.resolve_subroutine(sub.name)
    gen = run_subroutine(ctx, sub.name, steps)
    while next(gen):
        yield


def _dispatch_action(ctx: TestContext, action):
    if isinstance(action, SubCall):
        yield from _run_subcall(ctx, action)
    else:
        yield from _run_action(ctx, action)


def _checks_ok(ctx: TestContext, step: Step):
    is_ok = True
    for chk in step.checks:
        if not _compare(ctx.var(chk.var).Value, chk.op, chk.exp_value):
            is_ok = False
    return is_ok


def _judge_loop(ctx: TestContext, step: Step, time_st, wait_value):
    """
    Shared judge/wait loop.  Yields `wait_value` while waiting (None for a
    top-level test, True for a subroutine).  Returns the pass/fail result.
    `time_st` is the step start time (recorded before any actions), so the
    timeout / watch window includes time spent in lib calls, exactly like judge.
    """
    if step.method == 'watch':
        limit = float(step.watch_ms) / 1000 - ctx.stepsize()
        while True:
            is_ok = _checks_ok(ctx, step)
            if not is_ok:
                break
            if not (round(ctx._time.Value - time_st, ctx._digit) < limit):
                break
            yield wait_value
    else:
        while True:
            is_ok = _checks_ok(ctx, step)
            if is_ok:
                break
            if not (round(ctx._time.Value - time_st, ctx._digit) < step.timeout):
                is_ok = False
                break
            yield wait_value
    return is_ok


# --------------------------------------------------------------------------- #
# The generic MainGenerator  (replaces judge's hand-written per-step code)
# --------------------------------------------------------------------------- #
def run_test(ctx: TestContext, steps: List[Step]):
    """
    Generator: identical control flow to judge's MainGenerator, but driven by
    the `steps` data instead of copy-pasted per-step blocks.
    """
    log = ctx._log

    ctx.test_python_over = -1
    ctx.test_step_no = -1
    ctx.test_result = -1

    for step in steps:
        ctx.test_step_no = step.no
        ctx.current_step = step
        time_st = ctx._time.Value

        # --- header ------------------------------------------------------- #
        header = '-------------------Step%d-------------------' % step.no
        print(header)
        if step.header_log:
            log.info(header)
        if step.category:
            log.info(step.category)
        if step.comment:
            log.info('Step%d %s' % (step.no, step.comment))

        # --- apply inputs ------------------------------------------------- #
        for asg in step.inputs:
            ctx.var(asg.var).Value = asg.value

        # --- lib calls / subroutines before judging ----------------------- #
        # NOTE: time_st stays at the step start (judge records it once), so a
        # step's timeout / watch window includes any time spent in actions.
        for action in step.actions:
            yield from _dispatch_action(ctx, action)

        # --- judge loop --------------------------------------------------- #
        is_ok = yield from _judge_loop(ctx, step, time_st, None)

        # --- detail output ------------------------------------------------ #
        _emit_step_detail(ctx, step)

        # --- per-step result ---------------------------------------------- #
        if is_ok:
            print('Step.%d is passed.' % step.no)
            log.info('Step.%d is passed at %ss.' % (step.no, ctx.now()))
        else:
            print('Step.%d is failed.' % step.no)
            log.info('Step.%d is failed at %ss.' % (step.no, ctx.now()))
            ctx.test_result = step.no
            break

    # --- overall result --------------------------------------------------- #
    if ctx.test_result == -1:
        print('Test is over. All steps is verified.')
        log.info('All steps are verified.Test is Passed.')
    else:
        print('Test is failed in Step%d!!!!' % ctx.test_result)
        log.info('Test is failed in Step%d!!!!' % ctx.test_result)
    print('Test is stoped at%ss.' % ctx.now())

    ctx.test_python_over = 0
    for _ in range(10):
        yield


def run_subroutine(ctx: TestContext, name, steps):
    """
    Run a JSON-defined subroutine, reproducing the legacy SetTrigger_* output
    flavor exactly:
      * ' Subroutine(<name>) is started!' / ' ... is ended!'   (log only)
      * per-step header '------------------- Subroutine(<name>) StepN-------------------'
      * comment line    ' Subroutine(<name>) StepN <comment>'
      * result lines    '<name> Step.N is passed.' etc.
      * overall print-only summary (no 'All steps verified' log line)
    Protocol: yields True while waiting, yields False when finished (so the
    caller's `while next(gen): yield` drives it just like the generated code).
    """
    log = ctx._log

    ctx.test_python_over = -1
    ctx.test_step_no = -1

    log.info(' Subroutine(%s) is started!' % name)

    test_result = -1
    for step in steps:
        ctx.test_step_no = step.no
        ctx.current_step = step
        time_st = ctx._time.Value

        header = '------------------- Subroutine(%s) Step%d-------------------' % (name, step.no)
        print(header)
        log.info(header)
        if step.category:
            log.info(step.category)
        if step.comment:
            log.info(' Subroutine(%s) Step%d %s' % (name, step.no, step.comment))

        for asg in step.inputs:
            ctx.var(asg.var).Value = asg.value

        for action in step.actions:
            yield from _dispatch_action(ctx, action)

        is_ok = yield from _judge_loop(ctx, step, time_st, True)

        _emit_step_detail(ctx, step)

        if is_ok:
            print('%s Step.%d is passed.' % (name, step.no))
            log.info('%s Step.%d is passed at %ss.' % (name, step.no, ctx.now()))
        else:
            print('%s Step.%d is failed.' % (name, step.no))
            log.info('%s Step.%d is failed at %ss.' % (name, step.no, ctx.now()))
            test_result = step.no
            break

    if test_result == -1:
        print('Test is over. All steps is verified.')
    else:
        print('Test is failed in Step%d!!!!' % test_result)
    print('Test is stoped at%ss.' % ctx.now())

    ctx.test_python_over = 0
    log.info(' Subroutine(%s) is ended!' % name)

    yield False


def run_init_subroutine(ctx: TestContext, name, steps):
    """
    Run a JSON-defined *initialization* subroutine (e.g. SystemInitialize),
    reproducing the generated init-lib output flavor exactly:
      * ' Subroutine(<name>) is started!'                        (log only)
      * per-step header '------------------- Subroutine(<name>) StepN-------------------'
        (print + log)
      * category line (log only)
      * comment line ' Subroutine(<name>) StepN <comment>'       (log only)
      * inputs are assigned
    Unlike a process subroutine it does NOT judge, emits no per-step result
    line, no ' is ended!' line and no print-only summary.  It is called
    synchronously from pre_init (not a generator).
    """
    log = ctx._log

    ctx.test_python_over = -1
    ctx.test_step_no = -1

    log.info(' Subroutine(%s) is started!' % name)

    for step in steps:
        ctx.test_step_no = step.no
        ctx.current_step = step

        header = '------------------- Subroutine(%s) Step%d-------------------' % (name, step.no)
        print(header)
        log.info(header)
        if step.category:
            log.info(step.category)
        if step.comment:
            log.info(' Subroutine(%s) Step%d %s' % (name, step.no, step.comment))

        for asg in step.inputs:
            ctx.var(asg.var).Value = asg.value


def run_cleanup(ctx: TestContext, time):
    """
    Equivalent of judge's pre_cleanup: if the test was suspended before its
    expected values were met, re-emit the current step detail + failure lines.
    """
    log = ctx._log
    if ctx.test_python_over == 0:
        return ctx._DLL_OK

    print('The test was suspended !!!')
    log.info('The test was suspended !!!')

    step = ctx.current_step
    if step is not None:
        _emit_step_detail(ctx, step)
        n = ctx.test_step_no
        print('Step.%d is failed.' % n)
        log.info('Step.%d is failed at %ss.' % (n, time))
        print('Test is failed in Step%d!!!!' % n)
        log.info('Test is failed in Step%d!!!!' % n)

    return ctx._DLL_OK
