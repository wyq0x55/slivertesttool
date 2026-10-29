"""Browse the execution server's file system for the project binding pickers.

The binding's source root and index CSV are paths on the machine that runs
ut-agent, so a browser file dialog cannot produce them.  This lists one
directory at a time: names and kinds only, never file contents.  A project
editor can already bind any path the server can read, so listing names does
not widen what the binding can reach.
"""

from __future__ import annotations

import os
import string
from typing import Any

from ..lanmatrix.errors import ServiceError

KINDS = ("directory", "csv")
_ENTRY_LIMIT = 2000


def _roots() -> list[dict[str, Any]]:
    if os.name != "nt":
        return [{"name": "/", "path": "/", "type": "directory"}]
    drives = (f"{letter}:\\" for letter in string.ascii_uppercase)
    return [{"name": drive, "path": drive, "type": "directory"}
            for drive in drives if os.path.isdir(drive)]


def _parent(directory: str) -> str:
    parent = os.path.dirname(directory)
    # A drive or file-system root is its own parent; above it is the root list.
    return "" if parent == directory else parent


def _start(path: str) -> tuple[str, str | None]:
    """The directory to list for *path*, and the entry to mark as selected.

    A path that no longer exists opens at its nearest existing ancestor, so a
    stale binding still lands near where it used to point.
    """
    current = os.path.abspath(path)
    selected = None
    if os.path.isfile(current):
        current, selected = os.path.dirname(current), current
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return "", None
        current = parent
    return current, selected


def browse(path: str, kind: str) -> dict[str, Any]:
    """List the sub-directories (and, for ``csv``, CSV files) of *path*.

    An empty *path* lists the file-system roots (drives on Windows).
    """
    if kind not in KINDS:
        raise ServiceError(f"不支持的浏览类型：{kind}", code="VALIDATION_ERROR")
    path = (path or "").strip()
    if path and not os.path.isabs(path):
        raise ServiceError("请输入执行服务器上的绝对路径", code="VALIDATION_ERROR")
    directory, selected = _start(path) if path else ("", None)
    if not directory:
        return {"path": "", "parent": None, "selected": None,
                "entries": _roots(), "truncated": False}
    entries = []
    try:
        with os.scandir(directory) as items:
            for item in items:
                try:
                    if item.is_dir():
                        entries.append({"name": item.name, "path": item.path, "type": "directory"})
                    elif kind == "csv" and item.name.lower().endswith(".csv") and item.is_file():
                        entries.append({"name": item.name, "path": item.path, "type": "file"})
                except OSError:
                    continue
    except PermissionError as exc:
        # Not PERMISSION_DENIED: that code means the platform role check (403).
        raise ServiceError(f"执行服务器无权读取该目录：{directory}", code="VALIDATION_ERROR") from exc
    except OSError as exc:
        raise ServiceError(f"无法读取该目录：{directory}（{exc.strerror or exc}）",
                           code="VALIDATION_ERROR") from exc
    entries.sort(key=lambda entry: (entry["type"] != "directory", entry["name"].lower()))
    return {"path": directory, "parent": _parent(directory), "selected": selected,
            "entries": entries[:_ENTRY_LIMIT], "truncated": len(entries) > _ENTRY_LIMIT}
