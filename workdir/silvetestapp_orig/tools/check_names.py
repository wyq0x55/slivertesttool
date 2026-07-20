"""Static undefined-name checker (pure stdlib, no third-party deps).

Uses :mod:`symtable` to find names that a module reads from its *global*
(module) scope but never defines, imports, or receives as a parameter — the
exact failure mode of a hand-split module that forgot an import. Builtins are
ignored. This is a safety net for refactors we cannot validate with pytest.

Usage: python3 check_names.py <file.py> [<file.py> ...]
Exit code 0 = clean, 1 = at least one undefined global found.
"""
from __future__ import annotations

import builtins
import symtable
import sys

_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__loader__",
    "__spec__", "__builtins__", "__annotations__", "__dict__", "__class__",
}


def _collect_module_globals(top: symtable.SymbolTable) -> set[str]:
    names: set[str] = set()
    for sym in top.get_symbols():
        if sym.is_imported() or sym.is_assigned() or sym.is_namespace():
            names.add(sym.get_name())
    return names


def _walk(table: symtable.SymbolTable, module_globals: set[str],
          problems: list[tuple[str, str]]) -> None:
    for sym in table.get_symbols():
        name = sym.get_name()
        # A name resolved to the module/global scope but not defined there.
        refers_global = sym.is_global() or (
            table.get_type() == "module"
            and sym.is_referenced()
            and not sym.is_assigned()
            and not sym.is_imported()
            and not sym.is_parameter()
        )
        if refers_global and name not in module_globals and name not in _BUILTINS:
            problems.append((table.get_name() or "<module>", name))
    for child in table.get_children():
        _walk(child, module_globals, problems)


def check(path: str) -> list[tuple[str, str]]:
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    top = symtable.symtable(src, path, "exec")
    module_globals = _collect_module_globals(top)
    problems: list[tuple[str, str]] = []
    _walk(top, module_globals, problems)
    # De-dup while preserving order.
    seen = set()
    out = []
    for scope, name in problems:
        key = (scope, name)
        if key not in seen:
            seen.add(key)
            out.append((scope, name))
    return out


def main(argv: list[str]) -> int:
    rc = 0
    for path in argv:
        problems = check(path)
        if problems:
            rc = 1
            print(f"[FAIL] {path}")
            for scope, name in problems:
                print(f"    undefined global '{name}' (used in scope: {scope})")
        else:
            print(f"[ok]   {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
