"""Regression: an interval / range value cell may carry a 和名 prefix and use
constant names for its bounds, in either the ``[`` (inclusive) or ``(``
(exclusive) form.

Reproduces the reported request where

    カウント開始(U1G_DATA_ZERO,U2G_DAT_MAX)   -> (0, 65535)   both bounds exclusive
    カウント開始[U1G_DATA_ZERO,U2G_DAT_MAX)   -> [0, 65535)   lower inclusive

Previously the 和名-prefixed cell was mis-parsed by the export splitter: the
``(lo,hi)`` form was collapsed into a single comma-identifier and the ``[lo,hi)``
form was passed through whole, so neither reached the runner as an interval.

Both modules are loaded without importing the Flask app / vendor Silver runtime:
* the exporter has package-relative imports, so its parent packages are stubbed;
* the runner runs a CLI at import time, so only its definitions section is
  exec'd (everything above the module-level ``_SCRIPT_DIR = ...`` bootstrap).
"""
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPORT_PATH = os.path.join(
    _ROOT, "app", "services", "lanmatrix", "silver_json_export.py")
_RUNNER_PATH = os.path.join(
    _ROOT, "app", "runners", "silver_json", "silver_json_runner.py")


def _stub_pkg(name):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m
    return m


def _load_export():
    packages = (
        "app", "app.models", "app.runners", "app.runners.silver_json",
        "app.services", "app.services.lanmatrix",
    )
    name = "app.services.lanmatrix.silver_json_export"
    missing = object()
    module_names = packages + (name,)
    previous_modules = {
        module_name: sys.modules.get(module_name, missing)
        for module_name in module_names
    }
    parent_bindings = []
    for parent_name, attribute in (
        ("app", "models"), ("app", "runners"), ("app", "services"),
        ("app.models", "TestItemRow"), ("app.runners", "silver_json"),
        ("app.services", "lanmatrix"),
        ("app.services.lanmatrix", "silver_json_export"),
    ):
        parent = previous_modules.get(parent_name, missing)
        if parent is not missing:
            parent_bindings.append(
                (parent, attribute, getattr(parent, attribute, missing)))

    try:
        for pkg in packages:
            if pkg not in sys.modules:
                _stub_pkg(pkg)
        models_pkg = sys.modules["app.models"]
        if not hasattr(models_pkg, "TestItemRow"):
            models_pkg.TestItemRow = object
        runners_pkg = sys.modules["app.runners"]
        runners_pkg.silver_json = sys.modules["app.runners.silver_json"]
        spec = importlib.util.spec_from_file_location(name, _EXPORT_PATH)
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "app.services.lanmatrix"
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for parent, attribute, previous in reversed(parent_bindings):
            if previous is missing:
                if hasattr(parent, attribute):
                    delattr(parent, attribute)
            else:
                setattr(parent, attribute, previous)
        for module_name in reversed(module_names):
            previous = previous_modules[module_name]
            if previous is missing:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous


def _load_runner_defs():
    src = open(_RUNNER_PATH, encoding="utf-8").read()
    marker = "\n_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))"
    src = src[: src.index(marker)]
    src = (src
           .replace("from synopsys.silver import *", "pass")
           .replace("from synopsys.util import scheduler", "scheduler = None")
           .replace("from qtronic.silver import *", "pass")
           .replace("from qtronic.util import scheduler", "scheduler = None")
           .replace("from framework_builtins import BUILTINS, MagicList",
                    "BUILTINS = {}; MagicList = list"))
    ns = {"__name__": "silver_json_runner_defs", "__file__": _RUNNER_PATH}
    sys.path.insert(0, os.path.dirname(_RUNNER_PATH))
    exec(compile(src, _RUNNER_PATH, "exec"), ns)
    return types.SimpleNamespace(**ns)


def test_export_loader_preserves_the_live_model_class():
    from app.models import TestItemRow

    module_names = (
        "app",
        "app.models",
        "app.runners",
        "app.runners.silver_json",
        "app.services",
        "app.services.lanmatrix",
        "app.services.lanmatrix.silver_json_export",
    )
    before = {name: sys.modules.get(name) for name in module_names}

    _load_export()

    assert sys.modules["app.models"].TestItemRow is TestItemRow
    assert all(sys.modules.get(name) is module for name, module in before.items())


def test_split_cell_recognises_prefixed_and_bare_intervals():
    exp = _load_export()
    split = exp._split_cell
    assert split("カウント開始(U1G_DATA_ZERO,U2G_DAT_MAX)") == \
        ("カウント開始", "(U1G_DATA_ZERO,U2G_DAT_MAX)")
    assert split("カウント開始[U1G_DATA_ZERO,U2G_DAT_MAX)") == \
        ("カウント開始", "[U1G_DATA_ZERO,U2G_DAT_MAX)")
    assert split("[0,50)") == ("", "[0,50)")
    assert split("(0,50]") == ("", "(0,50]")
    # A plain 和名(識別子) cell (no comma) is NOT an interval.
    assert split("速度(V_MAX)") == ("速度", "V_MAX")
    assert split("V_MAX") == ("", "V_MAX")


def test_runner_resolves_const_bounds_and_bracket_inclusivity():
    run = _load_runner_defs()
    consts = {"U1G_DATA_ZERO": 0, "U2G_DAT_MAX": 65535}
    cases = {
        "(U1G_DATA_ZERO,U2G_DAT_MAX)": (0, 65535, False, False),
        "[U1G_DATA_ZERO,U2G_DAT_MAX)": (0, 65535, True, False),
        "[U1G_DATA_ZERO,U2G_DAT_MAX]": (0, 65535, True, True),
        "(U1G_DATA_ZERO,U2G_DAT_MAX]": (0, 65535, False, True),
    }
    for ident, want in cases.items():
        assert run._looks_like_interval(ident), ident
        assert run._parse_interval(ident, consts) == want, ident


def test_end_to_end_prefixed_interval():
    """The exporter's interval ident feeds straight into the runner parser."""
    exp = _load_export()
    run = _load_runner_defs()
    consts = {"U1G_DATA_ZERO": 0, "U2G_DAT_MAX": 65535}

    jname, ident = exp._split_cell("カウント開始[U1G_DATA_ZERO,U2G_DAT_MAX)")
    assert jname == "カウント開始"
    assert run._looks_like_interval(ident)
    assert run._parse_interval(ident, consts) == (0, 65535, True, False)


if __name__ == "__main__":
    test_split_cell_recognises_prefixed_and_bare_intervals()
    test_runner_resolves_const_bounds_and_bracket_inclusivity()
    test_end_to_end_prefixed_interval()
    print("all silver_json interval regression tests passed")
