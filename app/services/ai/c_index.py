"""C source indexer (clang AST) — the deterministic layer under procedure/SBS generation.

LLM prompts must only ever contain variable names that really exist, so the
module-scoped variable / function inventory is extracted by *code*, not by the
model. Parsing uses libclang (``pip install libclang``, native library
bundled), which gives the preprocessed, scope-aware truth:

* ``#ifdef`` blocks are resolved with the supplied compile args, so the index
  reflects the build configuration the code is actually compiled with;
* file-scope declarations are told apart from block locals by the AST, not by
  indentation heuristics;
* multi-declarator lines (``int a, b;``), qualifiers and array dims come out
  structurally instead of via regex approximation.

Content is parsed in-memory (unsaved files), so callers hand over source text
they got from anywhere — no files need to exist on disk. A small prologue of
the common embedded fixed-width typedefs is prepended when the source does not
include ``<stdint.h>``, so bare snippets without their real headers still
parse.

Index shape (unchanged from the previous regex backend — the consumers in
``scenarios.py`` and the prompts depend on it)::

    {
      "files": {
        "engine.c": {
          "globals": [{"name": ..., "type": ..., "array": bool}],
          "functions": [{"name": ..., "signature": ...}]
        }
      },
      "variables": {"engine_speed": "uint16_t", ...},   # name -> type
      "functions": {"engine_init": "void engine_init(void)"}
    }
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from clang.cindex import CursorKind, Index, TranslationUnit, TypeKind

# Prepended when the source doesn't bring its own <stdint.h>: bare snippets
# (a single function pasted from the review screen, an .c file whose headers
# live on the build server) still parse with the usual fixed-width types.
_STDINT_PROLOGUE = """
typedef signed char int8_t;
typedef unsigned char uint8_t;
typedef signed short int16_t;
typedef unsigned short uint16_t;
typedef signed int int32_t;
typedef unsigned int uint32_t;
typedef signed long long int64_t;
typedef unsigned long long uint64_t;
"""

# NOTE: deliberately NOT TranslationUnit.PARSE_SKIP_FUNCTION_BODIES — with
# bodies skipped, function definitions degrade to plain declarations and
# ``is_definition()`` returns False, dropping every function from the index.
_PARSE_FLAGS = 0


def _parse_one(index: Index, name: str, content: str,
               args: list[str]) -> Optional[Any]:
    text = content or ""
    if "stdint.h" not in text and "typedef" not in text:
        text = _STDINT_PROLOGUE + text
    try:
        return index.parse(
            name, args=["-x", "c", *args],
            unsaved_files=[(name, text)], options=_PARSE_FLAGS)
    except Exception:  # noqa: BLE001 - a broken file must not break the batch
        return None


def index_source(files: dict[str, str],
                 *, compile_args: Optional[list[str]] = None) -> dict[str, Any]:
    """Index ``{filename: content}`` into the structure documented above.

    ``compile_args`` are forwarded to clang verbatim (``-D…`` / ``-I…``),
    ideally from the project's ``compile_commands.json`` — the same code can
    yield different visible variables under different target macros.
    """
    args = list(compile_args or [])
    clang_index = Index.create()
    result: dict[str, Any] = {"files": {}, "variables": {}, "functions": {}}

    for name, content in (files or {}).items():
        tu = _parse_one(clang_index, name, content, args)
        file_entry: dict[str, Any] = {"globals": [], "functions": []}
        if tu is not None:
            _collect(tu.cursor, name, file_entry, result)
        result["files"][name] = file_entry
    return result


def _in_main_file(cursor, main_name: str) -> bool:
    loc = cursor.location.file
    # Cursors from headers (or the injected prologue's implicit file) have a
    # different or missing source file; only the requested TU's own
    # declarations belong to the inventory.
    return loc is not None and (loc.name == main_name or str(loc) == main_name)


def _collect(root, main_name: str, file_entry: dict, result: dict) -> None:
    for cursor in root.get_children():
        if not _in_main_file(cursor, main_name):
            continue
        kind = cursor.kind
        if kind == CursorKind.VAR_DECL:
            # A VAR_DECL at file scope (a direct child of the TU) is a global;
            # block locals only appear under function children, which this
            # walk never descends into.
            name = cursor.spelling
            if not name:
                continue
            entry = {
                "name": name,
                "type": cursor.type.spelling,
                "array": cursor.type.kind in (TypeKind.CONSTANTARRAY,
                                              TypeKind.INCOMPLETEARRAY,
                                              TypeKind.VARIABLEARRAY,
                                              TypeKind.DEPENDENTSIZEDARRAY),
            }
            file_entry["globals"].append(entry)
            # static/extern storage distinction is visible via
            # cursor.storage_class; not surfaced in the inventory for now —
            # the Silver build loop, not the index, decides what is settable.
            result["variables"].setdefault(name, entry["type"])
        elif kind == CursorKind.FUNCTION_DECL and cursor.is_definition():
            name = cursor.spelling
            if not name:
                continue
            signature = f"{cursor.result_type.spelling} {name}({_signature(cursor)})"
            file_entry["functions"].append({"name": name,
                                            "signature": signature})
            result["functions"].setdefault(name, signature)


def _signature(cursor) -> str:
    """Rebuild ``ret name(param, ...)`` from the AST's parameter children."""
    params = [c.type.spelling for c in cursor.get_children()
              if c.kind == CursorKind.PARM_DECL]
    return ", ".join(params)


def variable_inventory(index: dict[str, Any]) -> list[str]:
    """Flat ``name : type`` lines for prompts."""
    return sorted(f"{name} : {ctype}"
                  for name, ctype in index.get("variables", {}).items())


def select_context(index: dict[str, Any], keywords: Iterable[str],
                   *, max_chars: int = 6000) -> dict[str, str]:
    """Pick the source files whose code mentions any keyword (token match).

    This is the token-control knob: procedures see only their module's
    relevant files, not the whole tree. File selection prefers more keyword
    hits and stops once the character budget is exhausted.
    """
    wanted = [k for k in (kw.strip() for kw in keywords) if k]
    scored: list[tuple[int, str]] = []
    for name, entry in index.get("files", {}).items():
        blob_parts: list[str] = [name]
        for fn in entry.get("functions", []):
            blob_parts.append(fn["name"])
        for g in entry.get("globals", []):
            blob_parts.append(g["name"])
        blob = "\n".join(blob_parts)
        score = sum(1 for k in wanted if k in blob)
        if score:
            scored.append((score, blob))
    scored.sort(key=lambda t: -t[0])
    picked: dict[str, str] = {}
    used = 0
    for _score, blob in scored:
        if used + len(blob) > max_chars:
            break
        # The first line of the blob is the filename.
        lines = blob.split("\n")
        picked[lines[0]] = blob
        used += len(blob)
    return picked
