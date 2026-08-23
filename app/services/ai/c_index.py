"""Lightweight C source indexer — the deterministic layer under procedure/SBS generation.

LLM prompts must only ever contain variable names that really exist, so the
module-scoped variable / function inventory is extracted by *code*, not by the
model. A libclang binding would be more precise, but adds a heavy binary
dependency this offline platform must not take; the regex pass below handles
the common embedded-C subset (file-scope declarations, statics, functions,
structs) well enough to seed prompts, and anything it misses is caught later
by the Silver build / dry-run validation loop.

Index shape::

    {
      "files": {
        "engine.c": {
          "globals": [{"name": ..., "type": ..., "array": bool}],
          "functions": [{"name": ..., "signature": ...}]
        }
      },
      "variables": {"engine_speed": "uint16", ...},   # name -> type, all files
      "functions": {"engine_init": "void engine_init(void)"}
    }
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_TYPEDEF_RE = re.compile(r"\btypedef\s+struct\b")
_GLOBAL_RE = re.compile(
    r"^[ \t]*(?:extern\s+)?(?:static\s+)?(?:const\s+)?"
    r"((?:unsigned\s+|signed\s+)?[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?:\*+\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*(\[[^\]]*\])?\s*[=;]",
    re.MULTILINE,
)
_FUNC_RE = re.compile(
    r"^[ \t]*(?:static\s+)?(?:const\s+)?"
    r"((?:unsigned\s+|signed\s+)?[A-Za-z_][A-Za-z0-9_]*)\s+\*?\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^;{)]*)\)\s*\{",
    re.MULTILINE,
)
_STRUCT_MEMBER_RE = re.compile(
    r"\{([^{}]*)\}\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*;"
)
_C_KEYWORDS = {
    "if", "else", "for", "while", "switch", "case", "default", "return",
    "break", "continue", "goto", "sizeof", "typedef", "struct", "union",
    "enum", "static", "extern", "const", "register", "volatile", "inline",
    "void", "int", "char", "long", "short", "float", "double",
    "unsigned", "signed",
}
_NOISE_HEADERS = ("#",)


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*[\s\S]*?\*/", "", source)
    source = re.sub(r"//[^\n]*", "", source)
    return source


def index_source(files: dict[str, str]) -> dict[str, Any]:
    """Index ``{filename: content}`` into the structure documented above."""
    result: dict[str, Any] = {"files": {}, "variables": {}, "functions": {}}
    for name, content in (files or {}).items():
        text = _strip_comments(content or "")
        file_entry: dict[str, Any] = {"globals": [], "functions": []}
        for match in _FUNC_RE.finditer(text):
            ret, fname, params = match.group(1), match.group(2), match.group(3)
            if fname in _C_KEYWORDS:
                continue
            signature = f"{ret.strip()} {fname}({params.strip()})"
            file_entry["functions"].append(
                {"name": fname, "signature": signature})
            result["functions"].setdefault(fname, signature)
        for match in _GLOBAL_RE.finditer(text):
            ctype, vname, array = match.group(1), match.group(2), match.group(3)
            if vname in _C_KEYWORDS or ctype in _C_KEYWORDS and vname == ctype:
                continue
            # A global declaration must not sit inside a function body; the
            # regex anchors to line starts, and true block-locals are rare at
            # column 0 in embedded code — imprecision is acceptable (the build
            # loop catches it) as long as we don't invent names.
            file_entry["globals"].append({
                "name": vname,
                "type": ctype,
                "array": bool(array),
            })
            result["variables"].setdefault(vname, ctype)
        result["files"][name] = file_entry
    return result


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
