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
1. :func:`discover_search_paths` executes only the *prelude* of the judge (the
   statements before the first local import) with the Silver packages stubbed
   out, and records every directory the judge appends to ``sys.path``. Those are
   the library search roots.
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
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
_BUNDLE_VERSION = "2"
_BUNDLE_MARKER = (
    "# This judge.py was preprocessed by SilverTestApp's judge-bundler "
    "(v" + _BUNDLE_VERSION + ")."
)
# Separator line the bootstrap emits just before the original judge source; used
# to recover the original when re-bundling a stale/older bundle.
_ORIGINAL_SEP = "# ============================ original judge.py =============================="

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


def _make_silver_stub(name: str) -> "object":
    import types

    mod = types.ModuleType(name)
    mod.__path__ = []  # mark as package so submodule imports resolve
    mod.__all__ = []
    return mod


def discover_search_paths(judge_path: Path,
                          source: Optional[str] = None) -> List[Path]:
    """Discover the library directories a judge adds to ``sys.path``.

    The judge's prelude (everything up to its first local import) is executed in
    an isolated namespace with the Silver packages stubbed out; every directory
    appended to ``sys.path`` during that execution is returned (in order, only
    existing directories). *source*, when given, is parsed instead of the file's
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
    prelude = ast.Module(body=tree.body[:boundary], type_ignores=[])
    ast.fix_missing_locations(prelude)

    stub_names = [
        "synopsys", "synopsys.silver", "synopsys.util", "synopsys.util.scheduler",
        "qtronic", "qtronic.silver", "qtronic.util", "qtronic.util.scheduler",
    ]
    saved_modules = {n: sys.modules.get(n) for n in stub_names}
    saved_path = list(sys.path)
    added: List[Path] = []
    try:
        for n in stub_names:
            sys.modules[n] = _make_silver_stub(n)
        namespace = {
            "__name__": "__judge_prelude__",
            "__file__": str(judge_path),
            "__builtins__": __builtins__,
        }
        code = compile(prelude, f"<prelude:{judge_path.name}>", "exec")
        try:
            exec(code, namespace)  # noqa: S102 - user's own trusted judge prelude
        except Exception as exc:  # noqa: BLE001 - best-effort discovery
            raise BundleError(
                f"could not evaluate library paths from {judge_path.name}: {exc}"
            ) from exc
        for entry in sys.path:
            if entry in saved_path:
                continue
            p = Path(entry)
            if p.is_dir() and p not in added:
                added.append(p.resolve())
    finally:
        sys.path[:] = saved_path
        for n, prev in saved_modules.items():
            if prev is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = prev
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

    def exec_module(self, module):
        # Give the embedded module a ``__file__`` in the judge's directory so
        # code relying on ``__file__`` (path math, ``sys.path`` tweaks) works.
        if getattr(module, "__file__", None) is None:
            leaf = self._fullname.rsplit(".", 1)[-1]
            module.__file__ = _os.path.join(_JUDGE_DIR, leaf + ".py")
        code = compile(self._source, "<bundled:%s>" % self._fullname, "exec")
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

# ============================ original judge.py ==============================
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
    bootstrap = _BOOTSTRAP_TEMPLATE.format(
        marker=_BUNDLE_MARKER,
        module_list=", ".join(sorted(modules)) or "(none)",
        entries=entries,
    )
    return bootstrap + _strip_leading_coding(judge_src)


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
