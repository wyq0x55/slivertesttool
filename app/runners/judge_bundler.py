# coding: UTF-8
"""Self-contained ``judge.py`` bundler (client-side preprocessing).

A Silver ``judge.py`` typically pulls in project-local helper modules from
sibling ``Lib`` folders via ``sys.path.append(...)`` in its header, e.g.::

    from Common_Constant import *
    from Bit import *

Uploading those shared ``lib`` / ``LibValue`` folders to the service is
awkward. Instead, this module rewrites a ``judge.py`` into a **single
self-contained script**: every local library module it (transitively) imports
is embedded into the file, while Python standard-library and Silver
(``synopsys.*`` / ``qtronic.*``) imports are left untouched. The bundled script
therefore runs with no external ``Lib`` folders present.

How it works
------------
1. :func:`discover_search_paths` statically analyses the *prelude* of the judge
   (the statements before the first local import) via :mod:`ast` without
   executing any user code, evaluating ``__file__``, ``os.path`` and
   :class:`pathlib.Path` expressions passed to ``sys.path``. Those resolved
   directories form the library search roots.
2. :func:`bundle_judge` walks the import graph starting from the judge, resolves
   each imported name against the search roots, and embeds the source of every
   resolvable (local) module. Names that cannot be resolved locally -- stdlib,
   Silver, or third-party -- are assumed external and left as plain imports.
3. The embedded sources are attached through a tiny :mod:`importlib` meta-path
   finder emitted at the top of the output, so ``from X import *`` and package
   imports keep working exactly as before.
"""

from __future__ import annotations

import ast
import base64
import os
import re
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "bundle_judge",
    "bundle_judge_or_original",
    "discover_search_paths",
    "unresolved_local_imports",
    "is_bundled",
    "is_current_bundle",
    "BundleError",
]

# Marker written into every bundled judge; used to detect already-processed
# files. It carries a VERSION so an out-of-date bundle (produced by an older
# bootstrap) is re-processed instead of blindly kept -- otherwise a judge that
# was bundled by a buggy earlier version could never be fixed by re-uploading.
_BUNDLE_MARKER_BASE = "preprocessed by SilverTestApp's judge-bundler"
_BUNDLE_VERSION = "4"
_BUNDLE_MARKER = (
    "# This judge.py was preprocessed by SilverTestApp's judge-bundler "
    "(v" + _BUNDLE_VERSION + ")."
)
# Separator line legacy (v1-v3) bootstraps emitted before the original judge
# source; retained so older bundles still recover cleanly onto v4+.
_ORIGINAL_SEP = "# ============================ original judge.py =============================="
_ORIG_JUDGE_B64_RE = re.compile(
    r"^_ORIG_JUDGE_B64\s*=\s*(['\"])([A-Za-z0-9+/=]+)\1\s*$",
    re.MULTILINE,
)

# Silver's own packages are always treated as external (never inlined).
_SILVER_ROOTS = ("synopsys", "qtronic")

# Standard-library top-level package names. ``sys.stdlib_module_names`` exists on
# CPython 3.10+; fall back to a conservative built-in list otherwise.
_STDLIB_NAMES = set(getattr(sys, "stdlib_module_names", ())) | set(
    getattr(sys, "builtin_module_names", ())
)


class BundleError(Exception):
    """Raised when a judge cannot be bundled."""


def is_bundled(source: str) -> bool:
    """Return True if *source* was processed by the bundler (any version)."""
    return _BUNDLE_MARKER_BASE in source


def is_current_bundle(source: str) -> bool:
    """Return True if *source* was processed by the *current* bundler version."""
    return _BUNDLE_MARKER in source


def _recover_original(source: str) -> Optional[str]:
    """Recover the original judge source embedded in a bundle, if possible."""
    match = _ORIG_JUDGE_B64_RE.search(source)
    if match is not None:
        try:
            raw = base64.b64decode(match.group(2).encode("ascii"))
            return zlib.decompress(raw).decode("utf-8")
        except Exception:
            return None
    idx = source.find(_ORIGINAL_SEP)
    if idx == -1:
        return None
    nl = source.find("\n", idx)
    if nl == -1:
        return None
    return source[nl + 1:]


def _top(name: str) -> str:
    return name.split(".", 1)[0]


def _is_silver(name: str) -> bool:
    return _top(name) in _SILVER_ROOTS


def _is_stdlib(name: str) -> bool:
    return _top(name) in _STDLIB_NAMES


def _read_text(path: Path) -> str:
    # Silver judges are declared UTF-8; be tolerant of stray bytes. ``utf-8-sig``
    # transparently strips a leading UTF-8 BOM (files saved by Notepad / some
    # Windows editors) that would otherwise make ``ast.parse`` reject the source
    # with "invalid non-printable character U+FEFF". Any stray BOM/zero-width
    # no-break space still left at the very start is removed defensively.
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return text.lstrip("\ufeff")


# --------------------------------------------------------------------------- #
# Search-path discovery
# --------------------------------------------------------------------------- #
def _local_candidate(node: ast.AST) -> Optional[str]:
    """Return the module name if *node* is an import of a possibly-local module.

    An import is a "local candidate" when it is not a Silver import and not a
    standard-library import. ``from . import x`` (relative) also qualifies.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            if not _is_silver(alias.name) and not _is_stdlib(alias.name):
                return alias.name
        return None
    if isinstance(node, ast.ImportFrom):
        if node.level and node.level > 0:
            return node.module or "."
        mod = node.module or ""
        if mod and not _is_silver(mod) and not _is_stdlib(mod):
            return mod
    return None


_UNRESOLVED = object()
_OS_MOD = object()
_OS_PATH_MOD = object()
_SYS_MOD = object()
_SYS_PATH = object()
_PATHLIB_MOD = object()
_PATH_CTOR = object()

_SAFE_OS_PATH_FUNCS = {
    "abspath": os.path.abspath,
    " basename".strip(): os.path.basename,
    "dirname": os.path.dirname,
    "expanduser": os.path.expanduser,
    "join": os.path.join,
    "normcase": os.path.normcase,
    "normpath": os.path.normpath,
    "realpath": os.path.realpath,
}


class _StaticSysPathEvaluator:
    """Pure-AST evaluator for ``sys.path`` mutations in a judge prelude."""

    _MAX_STEPS = 512

    def __init__(self, judge_path: Path) -> None:
        self._judge_path = judge_path
        self._env: Dict[str, Any] = {
            "__file__": str(judge_path),
            "__name__": "__judge_prelude__",
        }
        self.entries: List[str] = []
        self._steps = 0

    def run(self, statements: List[ast.stmt]) -> None:
        for stmt in statements:
            if self._steps >= self._MAX_STEPS:
                break
            self._exec_stmt(stmt)

    def _exec_stmt(self, node: ast.stmt) -> None:
        self._steps += 1
        if self._steps > self._MAX_STEPS:
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._bind_import(alias.name, alias.asname)
        elif isinstance(node, ast.ImportFrom):
            self._bind_import_from(node)
        elif isinstance(node, ast.Assign):
            val = self._eval(node.value)
            for target in node.targets:
                self._assign_target(target, val)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            self._assign_target(node.target, self._eval(node.value))
        elif isinstance(node, ast.AugAssign):
            self._exec_aug_assign(node)
        elif isinstance(node, ast.Expr):
            self._eval(node.value)
        elif isinstance(node, ast.Try):
            self.run(node.body)
            for handler in node.handlers:
                self.run(handler.body)
            self.run(node.orelse)
            self.run(node.finalbody)
        elif isinstance(node, ast.If):
            cond = self._eval(node.test)
            if isinstance(cond, bool):
                self.run(node.body if cond else node.orelse)
            else:
                self.run(node.body)
                self.run(node.orelse)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            seq = self._eval(node.iter)
            if isinstance(seq, (list, tuple)) and len(seq) <= 32:
                for item in seq:
                    self._env[node.target.id] = item
                    self.run(node.body)

    def _bind_import(self, name: str, asname: Optional[str]) -> None:
        if name == "os":
            self._env[asname or "os"] = _OS_MOD
        elif name == "os.path":
            if asname:
                self._env[asname] = _OS_PATH_MOD
            else:
                self._env["os"] = _OS_MOD
        elif name == "sys":
            self._env[asname or "sys"] = _SYS_MOD
        elif name == "pathlib":
            self._env[asname or "pathlib"] = _PATHLIB_MOD

    def _bind_import_from(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if mod == "os":
                if alias.name == "path":
                    self._env[bound] = _OS_PATH_MOD
                elif alias.name in ("sep", "pardir", "curdir", "name", "fspath"):
                    self._env[bound] = getattr(os, alias.name)
            elif mod in ("os.path", "posixpath", "ntpath"):
                if alias.name in _SAFE_OS_PATH_FUNCS:
                    self._env[bound] = _SAFE_OS_PATH_FUNCS[alias.name]
            elif mod == "sys":
                if alias.name == "path":
                    self._env[bound] = _SYS_PATH
                elif alias.name == "platform":
                    self._env[bound] = sys.platform
            elif mod == "pathlib":
                if alias.name in ("Path", "PurePath", "PosixPath", "WindowsPath"):
                    self._env[bound] = _PATH_CTOR

    def _assign_target(self, target: ast.expr, val: Any) -> None:
        if val is _UNRESOLVED:
            return
        if isinstance(target, ast.Name):
            self._env[target.id] = val
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(val, (tuple, list)):
            if len(target.elts) == len(val):
                for sub_t, sub_v in zip(target.elts, val):
                    self._assign_target(sub_t, sub_v)

    def _exec_aug_assign(self, node: ast.AugAssign) -> None:
        if not isinstance(node.op, ast.Add):
            return
        rhs = self._eval(node.value)
        lhs = self._eval(node.target)
        if lhs is _SYS_PATH and isinstance(rhs, (list, tuple)):
            for item in rhs:
                self._record_path(item, insert_at=None)
        elif isinstance(node.target, ast.Name) and isinstance(lhs, str) and isinstance(rhs, str):
            self._env[node.target.id] = lhs + rhs

    def _record_path(self, value: Any, *, insert_at: Optional[int]) -> None:
        if isinstance(value, (str, Path)):
            text = str(value)
            if not text:
                return
            if insert_at is not None and insert_at <= 0:
                self.entries.insert(0, text)
            else:
                self.entries.append(text)

    def _eval(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, int, bool)) or node.value is None:
                return node.value
            return _UNRESOLVED
        if isinstance(node, ast.Name):
            if node.id == "str":
                return str
            return self._env.get(node.id, _UNRESOLVED)
        if isinstance(node, ast.JoinedStr):
            parts: List[str] = []
            for val in node.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    parts.append(val.value)
                elif isinstance(val, ast.FormattedValue) and val.format_spec is None:
                    inner = self._eval(val.value)
                    if not isinstance(inner, (str, Path, int)):
                        return _UNRESOLVED
                    parts.append(str(inner))
                else:
                    return _UNRESOLVED
            return "".join(parts)
        if isinstance(node, ast.List):
            items = [self._eval(e) for e in node.elts]
            return items if all(x is not _UNRESOLVED for x in items) else _UNRESOLVED
        if isinstance(node, ast.Tuple):
            items = [self._eval(e) for e in node.elts]
            return tuple(items) if all(x is not _UNRESOLVED for x in items) else _UNRESOLVED
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            operand = self._eval(node.operand)
            return -operand if type(operand) is int else _UNRESOLVED
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left = self._eval(node.left)
            right = self._eval(node.comparators[0])
            if left is not _UNRESOLVED and right is not _UNRESOLVED:
                if isinstance(node.ops[0], ast.Eq):
                    return left == right
                if isinstance(node.ops[0], ast.NotEq):
                    return left != right
            return _UNRESOLVED
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            if isinstance(node.op, ast.Div) and isinstance(left, Path) and isinstance(right, (str, Path)):
                return left / right
            if isinstance(node.op, ast.Add):
                if isinstance(left, str) and isinstance(right, str):
                    return left + right
                if isinstance(left, list) and isinstance(right, list):
                    return left + right
            return _UNRESOLVED
        if isinstance(node, ast.Subscript):
            base = self._eval(node.value)
            if base is _UNRESOLVED:
                return _UNRESOLVED
            if isinstance(node.slice, ast.Slice):
                lo = self._eval(node.slice.lower) if node.slice.lower else None
                hi = self._eval(node.slice.upper) if node.slice.upper else None
                if (lo is None or type(lo) is int) and (hi is None or type(hi) is int):
                    try:
                        return base[slice(lo, hi)]
                    except Exception:
                        return _UNRESOLVED
                return _UNRESOLVED
            idx = self._eval(node.slice)
            if type(idx) is int:
                try:
                    return base[idx]
                except Exception:
                    return _UNRESOLVED
            return _UNRESOLVED
        if isinstance(node, ast.Attribute):
            return self._eval_attr(self._eval(node.value), node.attr)
        if isinstance(node, ast.Call):
            return self._eval_call(node)
        return _UNRESOLVED

    def _eval_attr(self, owner: Any, attr: str) -> Any:
        if owner is _OS_MOD:
            if attr == "path":
                return _OS_PATH_MOD
            if attr in ("sep", "pardir", "curdir", "name", "fspath"):
                return getattr(os, attr)
        elif owner is _OS_PATH_MOD:
            return _SAFE_OS_PATH_FUNCS.get(attr, _UNRESOLVED)
        elif owner is _SYS_MOD:
            if attr == "path":
                return _SYS_PATH
            if attr == "platform":
                return sys.platform
        elif owner is _PATHLIB_MOD:
            if attr in ("Path", "PurePath", "PosixPath", "WindowsPath"):
                return _PATH_CTOR
        elif owner is _SYS_PATH:
            if attr in ("append", "insert", "extend"):
                return (_SYS_PATH, attr)
        elif isinstance(owner, Path):
            if attr == "parent":
                return owner.parent
            if attr == "parents":
                return tuple(owner.parents)
            if attr in ("name", "stem", "suffix"):
                return getattr(owner, attr)
            if attr in ("resolve", "absolute", "expanduser", "as_posix", "joinpath"):
                return (owner, attr)
        return _UNRESOLVED

    def _eval_call(self, node: ast.Call) -> Any:
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            return _UNRESOLVED
        if any(kw.arg is None for kw in node.keywords):
            return _UNRESOLVED
        func = self._eval(node.func)
        if func is _UNRESOLVED:
            return _UNRESOLVED
        args = [self._eval(arg) for arg in node.args]
        if any(arg is _UNRESOLVED for arg in args):
            return _UNRESOLVED
        if func is _PATH_CTOR:
            if all(isinstance(a, (str, Path)) for a in args):
                return Path(*args) if args else Path(".")
            return _UNRESOLVED
        if func is str and len(args) == 1 and isinstance(args[0], (str, Path, int)):
            return str(args[0])
        if func is os.fspath and len(args) == 1 and isinstance(args[0], (str, Path)):
            return os.fspath(args[0])
        if callable(func) and func in _SAFE_OS_PATH_FUNCS.values():
            if not node.keywords and args and all(isinstance(a, (str, Path)) for a in args):
                try:
                    return func(*[str(a) for a in args])
                except Exception:
                    return _UNRESOLVED
            return _UNRESOLVED
        if isinstance(func, tuple) and len(func) == 2:
            receiver, method = func
            if receiver is _SYS_PATH:
                if method == "append" and len(args) == 1:
                    self._record_path(args[0], insert_at=None)
                elif method == "insert" and len(args) == 2 and type(args[0]) is int:
                    self._record_path(args[1], insert_at=args[0])
                elif method == "extend" and len(args) == 1 and isinstance(args[0], (list, tuple)):
                    for item in args[0]:
                        self._record_path(item, insert_at=None)
                return None
            if isinstance(receiver, Path):
                if method in ("resolve", "absolute"):
                    return Path(os.path.abspath(receiver))
                if method == "expanduser":
                    return Path(os.path.expanduser(str(receiver)))
                if method == "as_posix":
                    return receiver.as_posix()
                if method == "joinpath" and all(isinstance(a, (str, Path)) for a in args):
                    return receiver.joinpath(*args)
        return _UNRESOLVED


def discover_search_paths(judge_path: Path,
                          source: Optional[str] = None) -> List[Path]:
    """Discover the library directories a judge adds to ``sys.path``.

    The judge's prelude (everything up to its first local import) is statically
    evaluated via :mod:`ast` with zero dynamic code execution; every directory
    added to ``sys.path`` is returned (in order, only existing directories).
    *source*, when given, is parsed instead of the file's
    current contents (used when re-bundling a stale bundle from its recovered
    original).
    """
    judge_path = Path(judge_path).resolve()
    src = source if source is not None else _read_text(judge_path)
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise BundleError(f"cannot parse {judge_path.name}: {exc}") from exc

    boundary = len(tree.body)
    for i, node in enumerate(tree.body):
        if _local_candidate(node) is not None:
            boundary = i
            break
    evaluator = _StaticSysPathEvaluator(judge_path)
    evaluator.run(tree.body[:boundary])

    added: List[Path] = []
    for entry in evaluator.entries:
        try:
            p = Path(entry)
            if not p.is_absolute():
                p = (judge_path.parent / p)
            if p.is_dir():
                resolved = p.resolve()
                if resolved not in added:
                    added.append(resolved)
        except OSError:
            continue
    return added


# --------------------------------------------------------------------------- #
# Import-graph collection
# --------------------------------------------------------------------------- #
def _imported_targets(source: str, pkg: str) -> List[str]:
    """Fully-qualified module names an AST references via imports.

    ``pkg`` is the package that *source* belongs to (``""`` for a top-level
    script/module), used to resolve relative imports.
    """
    targets: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return targets
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if level == 0:
                base = node.module or ""
                if base:
                    targets.append(base)
                    for alias in node.names:
                        if alias.name != "*":
                            targets.append(f"{base}.{alias.name}")
            else:
                # Relative import: climb ``level - 1`` packages from ``pkg``.
                parts = pkg.split(".") if pkg else []
                up = parts[: len(parts) - (level - 1)] if level - 1 <= len(parts) else []
                prefix = ".".join(up)
                base = ".".join(x for x in (prefix, node.module) if x)
                if base:
                    targets.append(base)
                for alias in node.names:
                    if alias.name != "*":
                        targets.append(".".join(x for x in (base, alias.name) if x))
    return targets


def _resolve(name: str, search_dirs: List[Path]) -> Optional[Tuple[Path, bool]]:
    """Resolve a dotted module *name* to a source file within *search_dirs*.

    Returns ``(path, is_package)`` or ``None`` when the name is not local.
    """
    if not name or _is_silver(name) or _is_stdlib(name):
        return None
    parts = name.split(".")
    for base in search_dirs:
        module_file = base.joinpath(*parts).with_suffix(".py")
        if module_file.is_file():
            return module_file, False
        pkg_init = base.joinpath(*parts, "__init__.py")
        if pkg_init.is_file():
            return pkg_init, True
    return None


def _collect(judge_src: str, search_dirs: List[Path]) -> "Dict[str, Tuple[str, bool]]":
    """Collect every local module reachable from the judge.

    Returns ``{fullname: (source, is_package)}``.
    """
    modules: Dict[str, Tuple[str, bool]] = {}
    seen: set = set()
    # (source, owning-package) work items.
    stack: List[Tuple[str, str]] = [(judge_src, "")]
    while stack:
        source, pkg = stack.pop()
        for target in _imported_targets(source, pkg):
            if target in seen:
                continue
            seen.add(target)
            resolved = _resolve(target, search_dirs)
            if resolved is None:
                continue
            path, is_pkg = resolved
            mod_source = _read_text(path)
            modules[target] = (mod_source, is_pkg)
            owning_pkg = target if is_pkg else target.rpartition(".")[0]
            stack.append((mod_source, owning_pkg))
    return modules


# --------------------------------------------------------------------------- #
# Output rendering
# --------------------------------------------------------------------------- #
_BOOTSTRAP_TEMPLATE = '''\
# coding: UTF-8
# =============================================================================
{marker}
# All project-local library modules it imports have been embedded below so the
# script is fully self-contained -- no external Lib / LibValue folders are
# required at runtime. Python standard-library and Silver (synopsys.* /
# qtronic.*) imports are left untouched.
#
# Embedded modules: {module_list}
# =============================================================================
import sys as _sys
import os as _os
import base64 as _base64
import zlib as _zlib
import linecache as _linecache
import importlib.util as _ilu
import importlib.abc as _ila

# The judge is executed from a file, so Silver defines ``__file__`` here. Capture
# it (and its directory) so each embedded module can be given a sensible
# ``__file__`` -- otherwise a library module that does e.g.
# ``os.path.dirname(__file__)`` at import time raises ``NameError``.
try:
    _JUDGE_FILE = _os.path.abspath(__file__)
except NameError:
    _JUDGE_FILE = _os.path.abspath("bundled_judge.py")
_JUDGE_DIR = _os.path.dirname(_JUDGE_FILE)

_BUNDLED_MODULES = {{
{entries}
}}


class _BundledLoader(_ila.Loader):
    def __init__(self, fullname, source):
        self._fullname = fullname
        self._source = source

    def create_module(self, spec):
        return None

    def get_source(self, fullname):
        if fullname == self._fullname:
            return self._source
        info = _BUNDLED_MODULES.get(fullname)
        if info is None:
            return None
        return _base64.b64decode(info[0]).decode("utf-8")

    def exec_module(self, module):
        # Give the embedded module a ``__file__`` in the judge's directory so
        # code relying on ``__file__`` (path math, ``sys.path`` tweaks) works.
        if getattr(module, "__file__", None) is None:
            leaf = self._fullname.rsplit(".", 1)[-1]
            module.__file__ = _os.path.join(_JUDGE_DIR, leaf + ".py")
        origin = "<bundled:%s>" % self._fullname
        _linecache.cache[origin] = (
            len(self._source),
            None,
            self._source.splitlines(True),
            origin,
        )
        code = compile(self._source, origin, "exec")
        exec(code, module.__dict__)


class _BundledFinder(_ila.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        info = _BUNDLED_MODULES.get(fullname)
        if info is None:
            return None
        source = _base64.b64decode(info[0]).decode("utf-8")
        loader = _BundledLoader(fullname, source)
        return _ilu.spec_from_loader(fullname, loader, is_package=info[1])


if not any(isinstance(_f, _BundledFinder) for _f in _sys.meta_path):
    _sys.meta_path.insert(0, _BundledFinder())

_ORIG_JUDGE_B64 = {orig_judge_b64!r}
_orig_src = _zlib.decompress(_base64.b64decode(_ORIG_JUDGE_B64)).decode("utf-8")
try:
    _judge_fname = __file__
except NameError:
    _judge_fname = _JUDGE_FILE
for _fname in (_judge_fname, _os.path.abspath(_judge_fname)):
    _linecache.cache[_fname] = (
        len(_orig_src),
        None,
        _orig_src.splitlines(True),
        _fname,
    )
exec(compile(_orig_src, _judge_fname, "exec"), globals())
'''


def _strip_leading_coding(source: str) -> str:
    """Drop a leading ``# coding:`` / ``# -*- coding ... -*-`` line.

    A PEP 263 encoding declaration is only honoured on the first two lines. The
    bundled output already declares UTF-8 at its very top, and the embedded
    sources are compiled from ``str`` objects (so their own declarations are
    irrelevant), so the judge's original declaration must be removed to avoid a
    duplicate/ineffective declaration mid-file.
    """
    lines = source.splitlines(keepends=True)
    out = []
    removed = False
    for idx, line in enumerate(lines):
        if not removed and idx < 2 and "coding" in line and line.lstrip().startswith("#"):
            removed = True
            continue
        out.append(line)
    return "".join(out)


def _render_entries(modules: "Dict[str, Tuple[str, bool]]") -> str:
    rows = []
    for name in sorted(modules):
        source, is_pkg = modules[name]
        b64 = base64.b64encode(source.encode("utf-8")).decode("ascii")
        rows.append(f"    {name!r}: ({b64!r}, {bool(is_pkg)!r}),")
    return "\n".join(rows)


def bundle_judge(judge_path: Path,
                 extra_search_dirs: Optional[List[Path]] = None) -> str:
    """Return the self-contained source for *judge_path*.

    Local library modules the judge imports (transitively) are embedded. Raises
    :class:`BundleError` on failure; callers may fall back to the original file.
    """
    judge_path = Path(judge_path)
    if not judge_path.is_file():
        raise BundleError(f"judge not found: {judge_path}")
    judge_src = _read_text(judge_path)

    # Idempotency + self-healing: a judge produced by the CURRENT bundler is left
    # as-is; one produced by an OLDER version is re-bundled from its recovered
    # original so bug fixes in the bootstrap reach previously-processed judges.
    if is_bundled(judge_src):
        if is_current_bundle(judge_src):
            return judge_src
        recovered = _recover_original(judge_src)
        if recovered is None:
            return judge_src
        judge_src = recovered

    search_dirs = _search_dirs_for(judge_path, extra_search_dirs, source=judge_src)

    modules = _collect(judge_src, search_dirs)
    if not modules:
        # Nothing local to embed: the judge is already self-contained.
        return judge_src

    entries = _render_entries(modules)
    orig_judge_b64 = base64.b64encode(
        zlib.compress(judge_src.encode("utf-8"))
    ).decode("ascii")
    bootstrap = _BOOTSTRAP_TEMPLATE.format(
        marker=_BUNDLE_MARKER,
        module_list=", ".join(sorted(modules)) or "(none)",
        entries=entries,
        orig_judge_b64=orig_judge_b64,
    )
    return bootstrap


def _search_dirs_for(judge_path: Path,
                     extra_search_dirs: Optional[List[Path]],
                     source: Optional[str] = None) -> List[Path]:
    """Search roots for a judge: its own discovered paths plus the extras."""
    try:
        search_dirs = discover_search_paths(judge_path, source=source)
    except BundleError:
        search_dirs = []
    for extra in extra_search_dirs or []:
        p = Path(extra)
        if p.is_dir() and p.resolve() not in search_dirs:
            search_dirs.append(p.resolve())
    return search_dirs


def unresolved_local_imports(judge_path: Path,
                             extra_search_dirs: Optional[List[Path]] = None
                             ) -> List[str]:
    """Local-looking module names the judge imports but that cannot be resolved.

    These are imports that are neither standard-library nor Silver
    (``synopsys.*`` / ``qtronic.*``) yet resolve to no source file under any
    search root -- i.e. helper modules whose ``lib`` / ``stdlib`` folder was not
    uploaded (or was uploaded incompletely). Such imports are left as plain
    imports in the bundle and would raise ``ImportError`` at run time, so callers
    surface them as an upload warning. Returned sorted and de-duplicated.
    """
    judge_path = Path(judge_path)
    if not judge_path.is_file():
        return []
    src = _read_text(judge_path)
    if is_bundled(src):
        # Analyse the recovered original so stale bundles are still checked.
        recovered = _recover_original(src)
        if recovered is None:
            return []
        src = recovered
    search_dirs = _search_dirs_for(judge_path, extra_search_dirs, source=src)
    missing = set()
    for target in _imported_targets(src, ""):
        if _is_silver(target) or _is_stdlib(target):
            continue
        if _resolve(target, search_dirs) is None:
            missing.add(_top(target))
    return sorted(missing)


def bundle_judge_or_original(judge_path: Path,
                             extra_search_dirs: Optional[List[Path]] = None
                             ) -> Tuple[str, Optional[str]]:
    """Best-effort bundling.

    Returns ``(source, error)`` -- ``error`` is ``None`` on success, otherwise a
    human-readable reason and *source* is the unmodified original judge text.
    """
    try:
        return bundle_judge(judge_path, extra_search_dirs), None
    except BundleError as exc:
        return _read_text(Path(judge_path)), str(exc)
