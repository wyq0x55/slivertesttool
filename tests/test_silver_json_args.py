"""Regression: a subroutine invoked with positional ``args`` binds the caller's
actual argument (実引数) to its formal parameter (仮引数), instead of silently
falling back to the parameter default.

Reproduces the reported bug where a step calling
``SetTrigger_..._TimeSet`` with ``args: "585"`` ran the subroutine with 0.

The framework module depends only on the stdlib, so it is loaded directly by
file path to keep the test independent of the Flask app package.
"""
import importlib.util
import logging
import os

_FW_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "runners", "silver_json", "silver_test_framework.py",
)


def _load_framework():
    spec = importlib.util.spec_from_file_location("silver_test_framework_uut", _FW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ctx(fw, store):
    class MockVar:
        def __init__(self, name):
            self._name = name

        @property
        def Value(self):
            return store.get(self._name, 0)

        @Value.setter
        def Value(self, v):
            store[self._name] = v

    log = logging.getLogger("silver_json_args_test")
    log.addHandler(logging.NullHandler())
    ctx = fw.TestContext(MockVar, log, MockVar("currentTime"), 3, True,
                         stepsize=MockVar("modelStepSize"))
    return ctx, MockVar


def _drive(fw, ctx, steps, store):
    gen = fw.run_test(ctx, steps)
    for _ in gen:
        store["currentTime"] = round(store["currentTime"] + 0.001, 3)


def _register_settime(fw, ctx):
    """SetTime(tval): assigns OUT = tval and checks OUT == tval."""
    sub_steps = [
        fw.Step(
            no=1,
            inputs=[fw.Assign(var="OUT", value=0, param="tval")],
            checks=[fw.Check(var="OUT", label="out", exp_name="tval",
                             exp_value=0, exp_desc="", op="==", param="tval")],
            timeout=1.0,
        )
    ]
    ctx.register_subroutine("SetTime", sub_steps, ["tval"])


def test_caller_arg_is_bound_to_subroutine_param():
    fw = _load_framework()
    store = {"currentTime": 0.0, "modelStepSize": 0.001}
    ctx, _ = _make_ctx(fw, store)
    _register_settime(fw, ctx)

    steps = [fw.Step(no=1, actions=[fw.SubCall(name="SetTime", args=[585])],
                     timeout=1.0)]
    _drive(fw, ctx, steps, store)

    assert store["OUT"] == 585          # the input used the caller arg, not 0
    assert ctx.test_result == -1        # and the check (OUT == 585) passed


def test_missing_arg_falls_back_to_default():
    fw = _load_framework()
    store = {"currentTime": 0.0, "modelStepSize": 0.001, "OUT": -1}
    ctx, _ = _make_ctx(fw, store)
    _register_settime(fw, ctx)

    steps = [fw.Step(no=1, actions=[fw.SubCall(name="SetTime")], timeout=1.0)]
    _drive(fw, ctx, steps, store)

    assert store["OUT"] == 0            # no caller arg -> declared default


def test_param_binding_does_not_leak_after_subroutine():
    """A top-level check with the same 'param' name is unaffected once the
    subroutine returns (the binding frame is popped)."""
    fw = _load_framework()
    store = {"currentTime": 0.0, "modelStepSize": 0.001}
    ctx, _ = _make_ctx(fw, store)
    _register_settime(fw, ctx)

    # Step 1 calls SetTime(585); step 2 is a plain check with an unbound param
    # marker -> must use its own baked expected value, not the popped 585.
    store["FLAG"] = 7
    steps = [
        fw.Step(no=1, actions=[fw.SubCall(name="SetTime", args=[585])], timeout=1.0),
        fw.Step(no=2, checks=[fw.Check(var="FLAG", label="f", exp_name="7",
                                       exp_value=7, exp_desc="", op="==",
                                       param="tval")], timeout=1.0),
    ]
    _drive(fw, ctx, steps, store)
    assert ctx.test_result == -1        # step 2 compared FLAG(7) == 7, not 585


if __name__ == "__main__":
    test_caller_arg_is_bound_to_subroutine_param()
    test_missing_arg_falls_back_to_default()
    test_param_binding_does_not_leak_after_subroutine()
    print("all silver_json args regression tests passed")
