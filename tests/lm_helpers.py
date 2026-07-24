"""Load the Flask-independent lanmatrix modules without importing Flask.

The pure modules (security, fields, validation, batch, excel_io) use relative
imports (``from . import ...``). We register them under a synthetic package so
they can be unit-tested with just the stdlib + openpyxl, avoiding the Flask /
SQLAlchemy dependency that the full ``app`` package pulls in.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

LM_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "services" / "lanmatrix"
_PKG = "lmpure"
# Load order respects intra-package dependencies.
_MODULES = ["security", "fields", "batch", "validation", "excel_io",
            "testmatrix_bridge", "matrix_excel", "const_excel", "io_excel",
            "libconst_bridge"]


def load():
    if _PKG in sys.modules:
        return sys.modules[_PKG]
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(LM_DIR)]
    sys.modules[_PKG] = pkg
    for name in _MODULES:
        spec = importlib.util.spec_from_file_location(
            f"{_PKG}.{name}", LM_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{name}"] = mod
        spec.loader.exec_module(mod)
        setattr(pkg, name, mod)
    return pkg
