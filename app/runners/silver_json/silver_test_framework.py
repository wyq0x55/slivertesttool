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
    """One input assignment:  Variable(var).Value = value

    ``param`` names the subroutine formal parameter (仮引数) this input binds to,
    if any. When the subroutine is invoked with a caller argument for that
    parameter, the argument overrides ``value`` (which is only the declared
    default baked in at export time).
    """
    var: str
    value: Any
    param: Optional[str] = None


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
    param: Optional[str] = None   # formal parameter this expected value binds to


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
    args: List[Any] = field(default_factory=list)   # caller positional args (実引数)


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
        self._sub_params = {}              # name -> [param_name, ...] (仮引数 order)
        # Stack of {param_name: value} bindings, one frame per active subroutine
        # invocation. Inputs / checks that name a formal parameter read the
        # caller's argument from the top frame instead of the baked default.
        self._param_stack = []
        self._cache = {}
        self.results = {}                  # captured lib return values
        # Single-slot return channel: a subroutine (SubCall) writes its overall
        # verdict here right before it finishes so the driving step can fail when
        # a called subroutine (possibly nested) failed internally.
        self.sub_ok = True

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

    def register_subroutine(self, name, steps, params=None):
        self._subs[name] = steps
        self._sub_params[name] = list(params or [])

    def resolve_subroutine(self, name):
        steps = self._subs.get(name)
        if steps is None:
            raise KeyError('Unknown subroutine referenced in test: %r' % name)
        return steps

    def subroutine_params(self, name):
        """Ordered formal-parameter names (仮引数) declared by a subroutine."""
        return self._sub_params.get(name, [])

    # -- caller-argument binding (仮引数 <- 実引数) -------------------------- #
    def push_params(self, binding):
        self._param_stack.append(dict(binding or {}))

    def pop_params(self):
        if self._param_stack:
            self._param_stack.pop()

    def bind_arg(self, param, fallback):
        """Caller argument bound to ``param`` for the current subroutine
        invocation, or ``fallback`` (the baked default) when the parameter is
        unbound or this step is not running inside a subroutine call."""
        if param and self._param_stack:
            frame = self._param_stack[-1]
            if param in frame:
                return frame[param]
        return fallback

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
    expected = ctx.bind_arg(chk.param, chk.exp_value)
    ok = _compare(observed, chk.op, expected)

    print(' ' + chk.var + ' = ' + str(observed))
    log.info('●' if ok else '▲')
    log.info('    Monitoring target: ' + chk.label + ' ( ' + chk.var + ' ) ')
    if chk.op == 'in':
        exp_detail = ' ( exp in ' + chk.exp_name + ' ) '
    else:
        exp_detail = (' ( (exp' + chk.op + chk.exp_name +
                      '(' + str(expected) + ')) ) ')
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
        # Judge idiom, verbatim:  ``while next(gen): yield``.  The bare ``yield``
        # (None) is what hands control back to *Silver* so it advances the model
        # by one macro step -- this is how the lib's own wait/timeout (its default
        # 5 s judge window) actually elapses in *simulated* time. Yielding a
        # non-None token here does NOT step Silver's clock, which is why an
        # earlier ``yield True`` left simulated time at 0.0 s and made every
        # wait (nested or not) collapse instantly. A lib signals completion by
        # yielding a falsy value (Wait-style ``yield False``) or by ``return``;
        # a bare ``return`` makes ``next(gen)`` raise StopIteration, which inside
        # a generator becomes RuntimeError (PEP 479) and would surface as a
        # Silver DLL_ERROR, so we catch it and treat it as normal completion.
        try:
            while next(gen):
                yield
        except StopIteration:
            pass
    else:
        result = fn(*call.args)
        if call.store:
            ctx.results[call.store] = result


def _run_subcall(ctx: TestContext, sub: SubCall):
    """Drive a JSON-defined subroutine (possibly itself nesting more subcalls).

    Uses plain ``yield from`` rather than the ``while next(gen): yield`` judge
    idiom on purpose. ``run_subroutine`` is our own generator that already yields
    None (bare) while waiting and simply *returns* when finished, so ``yield
    from`` forwards every wait straight to Silver (stepping the model) and ends
    naturally when the subroutine returns. This is transparent to any depth of
    nesting: a lib-nested-lib chain waits correctly at every level instead of the
    inner wait being swallowed or the whole call being abandoned mid-wait, and
    it never turns a nested wait into a lost macro step (which previously left
    simulated time stuck at 0.0 s). The subroutine's pass/fail verdict is handed
    back out-of-band via ``ctx.sub_ok``.
    """
    steps = ctx.resolve_subroutine(sub.name)
    yield from run_subroutine(ctx, sub.name, steps, sub.args)


def _dispatch_action(ctx: TestContext, action):
    if isinstance(action, SubCall):
        yield from _run_subcall(ctx, action)
    else:
        yield from _run_action(ctx, action)


def _checks_ok(ctx: TestContext, step: Step):
    is_ok = True
    for chk in step.checks:
        expected = ctx.bind_arg(chk.param, chk.exp_value)
        if not _compare(ctx.var(chk.var).Value, chk.op, expected):
            is_ok = False
    return is_ok


def _judge_loop(ctx: TestContext, step: Step, time_st, wait_value):
    """
    Shared judge/wait loop.  Yields `wait_value` while waiting -- always None so
    every poll hands control back to Silver and advances the model by one macro
    step (that is how the step's timeout / watch window elapses in *simulated*
    time; a truthy token would freeze the clock).  Returns the pass/fail result.
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
        action_ok = True
        for action in step.actions:
            ctx.sub_ok = True
            yield from _dispatch_action(ctx, action)
            if not ctx.sub_ok:
                action_ok = False

        # --- judge loop --------------------------------------------------- #
        is_ok = yield from _judge_loop(ctx, step, time_st, None)
        # A called subroutine that failed internally (e.g. a lib whose own
        # check, or a nested lib's check, did not pass) must fail this step too;
        # otherwise the subroutine's verdict would be silently swallowed.
        if not action_ok:
            is_ok = False

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


def run_subroutine(ctx: TestContext, name, steps, args=None):
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

    # Bind the caller's positional arguments (実引数) to this subroutine's formal
    # parameters (仮引数) in declaration order, so its inputs / checks use the
    # passed value instead of the parameter default.
    _binding = {}
    _pnames = ctx.subroutine_params(name)
    _cargs = list(args or [])
    for _i, _pname in enumerate(_pnames):
        if _i < len(_cargs):
            _binding[_pname] = _cargs[_i]
    ctx.push_params(_binding)

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
            ctx.var(asg.var).Value = ctx.bind_arg(asg.param, asg.value)

        action_ok = True
        for action in step.actions:
            ctx.sub_ok = True
            yield from _dispatch_action(ctx, action)
            if not ctx.sub_ok:
                action_ok = False

        # Wait with a bare (None) token, exactly like the top-level test, so each
        # poll steps Silver's clock and the subroutine's own timeout window
        # elapses in simulated time (a truthy token would freeze the clock).
        is_ok = yield from _judge_loop(ctx, step, time_st, None)
        # Propagate a nested subroutine's failure up to this subroutine's step.
        if not action_ok:
            is_ok = False

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

    ctx.pop_params()

    ctx.test_python_over = 0
    log.info(' Subroutine(%s) is ended!' % name)

    # Hand this subroutine's overall verdict back to the calling step so a
    # failure (including one bubbled up from a nested subroutine) is not lost.
    # Completion is signalled by simply *returning* (the driving ``yield from``
    # ends), not by yielding a sentinel -- so no extra macro step is consumed and
    # the value can never be misread as "keep waiting".
    ctx.sub_ok = (test_result == -1)
    return


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
            ctx.var(asg.var).Value = ctx.bind_arg(asg.param, asg.value)


def run_cleanup(ctx: TestContext, time):
    """
    Equivalent of judge's pre_cleanup: if the test was suspended before its
    expected values were met, re-emit the current step detail + failure lines.
    """
    log = ctx._log
    if ctx.test_python_over == 0:
        return ctx._DLL_OK

    # Silver loads this module twice per run: once when ``add_module`` injects
    # it, and again after ``silver.restart()``. The first instance is discarded
    # without ever executing a step, and its teardown used to emit a full
    # "suspended -> Step.N is failed -> Test is failed" block into jdgrslt.log.
    # Those lines carry no "Test case ... is started!" marker, so whenever the
    # discarded instance was torn down *after* the real run had written its
    # result -- a matter of timing, which is why it only bit under load -- the
    # verdict parser attributed the failure to the real, passing case.
    # An instance that never reached a step has nothing to report.
    if ctx.current_step is None or ctx.test_step_no == -1:
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
