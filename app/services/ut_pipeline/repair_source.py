"""Verbatim source of the function under test, located by extractor provenance.

The span comes from the FunctionIR the generator already used, never from a
text search for the function name. The model may read the code to choose
input values; every proposed row is still replayed by ut-agent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

CONTRACT = "ut-platform.function-source/1"
MAX_SOURCE_CHARS = 24000


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp932", errors="replace")


def _globals(function: dict[str, Any]) -> list[dict[str, Any]]:
    keys = ("name", "is_const", "is_volatile", "array_sizes", "read", "write")
    return [{key: item.get(key) for key in keys}
            for item in function.get("global_objects") or () if isinstance(item, dict)]


def load(ir_json: Any, function: Optional[str]) -> Optional[dict[str, Any]]:
    """Return the function body and its global objects, or None without proof.

    Clang's end offset is the start of the closing brace, so the slice must
    end exactly on "}"; any other shape (for example a macro-built body) is
    left out rather than guessed.
    """
    if not ir_json or not function:
        return None
    try:
        document = json.loads(Path(ir_json).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    for item in document.get("functions") or ():
        if not isinstance(item, dict) or item.get("name") != function:
            continue
        provenance = item.get("provenance") or {}
        span = provenance.get("spelling") or {}
        path, start, end, line = (span.get(key) for key in ("file", "offset", "end_offset", "line"))
        if (provenance.get("macro_stack") or span != provenance.get("expansion")
                or not isinstance(path, str) or type(start) is not int or type(end) is not int
                or type(line) is not int or not 0 <= start < end or line < 1):
            return None
        try:
            data = Path(path).read_bytes()[start:end + 1]
        except OSError:
            return None
        if not data.endswith(b"}"):
            return None
        return {"contract": CONTRACT, "file": path, "line": line,
                "sha256": hashlib.sha256(data).hexdigest(), "text": _decode(data),
                "globals": _globals(item)}
    return None


def section(source: Optional[dict[str, Any]]) -> str:
    """Numbered source lines, kept whole or omitted whole."""
    if not source:
        return "## 被测函数源码\n未提供（无法由 extractor 定位源码范围），只依据下面的类型化证据。\n\n"
    lines = source["text"].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    width = len(str(source["line"] + len(lines) - 1))
    body = "\n".join(f"{number:>{width}}| {text}" for number, text in enumerate(lines, source["line"]))
    if len(body) > MAX_SOURCE_CHARS:
        return (f"## 被测函数源码\n源码 {len(body)} 字符，超过 {MAX_SOURCE_CHARS} 字符上限，未提供；"
                "只依据下面的类型化证据。\n\n")
    objects = json.dumps(source.get("globals") or [], ensure_ascii=False, sort_keys=True)
    return (f"## 被测函数源码（{Path(source['file']).name}，行号为原文件行号）\n"
            "源码只用于理解判定与数据流；数组下标超出 array_sizes 是未定义行为，"
            "不能据此推测期望值，这类义务写入 declined。\n"
            f"```c\n{body}\n```\n"
            f"全局对象（extractor 类型事实）：{objects}\n\n")
