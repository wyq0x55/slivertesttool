"""Sandboxed execution-server directory browsing for UT project bindings.

Browsing and non-admin bindings are strictly confined to configured
``UT_ALLOWED_SOURCE_ROOTS`` (or an isolated per-project directory under
``WORKSPACE_DIR`` when unconfigured). Every path check enforces lexical and
canonical containment and rejects symlinks / Windows reparse points via
:func:`app.services.run_evidence_service.checked_path`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from flask import current_app, has_app_context

from ..lanmatrix.errors import ServiceError
from ..lanmatrix.permissions import PermissionDenied
from ..run_evidence_service import EvidenceError, _reparse_component, checked_path

KINDS = ("directory", "csv")
_ENTRY_LIMIT = 2000


def parse_roots(raw: str | Iterable[str | Path] | None) -> list[Path]:
    """Parse ``UT_ALLOWED_SOURCE_ROOTS`` from config or environment format."""
    if raw is None:
        return []
    if isinstance(raw, (str, Path)):
        text = str(raw).replace("\n", ";")
        sep = ";" if os.name == "nt" else os.pathsep
        parts = [
            item.strip()
            for chunk in text.split(sep)
            for item in (chunk.split(";") if os.name != "nt" else [chunk])
            if item.strip()
        ]
    else:
        parts = [str(item).strip() for item in raw if str(item).strip()]
    roots: list[Path] = []
    seen: set[str] = set()
    for part in parts:
        candidate = Path(os.path.abspath(part))
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            roots.append(candidate)
    return roots


def _validated_roots(candidates: Sequence[str | Path]) -> list[Path]:
    valid: list[Path] = []
    for raw in candidates:
        root = Path(os.path.abspath(raw))
        try:
            checked_path(root, root, exists=False)
        except EvidenceError as exc:
            raise PermissionDenied("允许的源码根目录包含不安全的重解析点或无效路径") from exc
        valid.append(root)
    return valid


def configured_roots(config: dict[str, Any] | None = None,
                     *, project_id: int | None = None) -> list[Path]:
    """Return validated allowed source roots for *project_id*.

    When ``UT_ALLOWED_SOURCE_ROOTS`` is configured, those directories form the
    allowlist. Otherwise each project is isolated to its own subdirectory under
    ``WORKSPACE_DIR/ut_sources/p<id>`` so server-wide filesystem enumeration is
    never exposed by default.
    """
    cfg = config if config is not None else (current_app.config if has_app_context() else None)
    raw_roots = parse_roots(cfg.get("UT_ALLOWED_SOURCE_ROOTS") if cfg else os.environ.get("UT_ALLOWED_SOURCE_ROOTS"))
    if raw_roots:
        return _validated_roots(raw_roots)
    workspace = cfg.get("WORKSPACE_DIR") if cfg else None
    if not workspace:
        raise PermissionDenied("未配置允许访问的执行服务器源码根目录（UT_ALLOWED_SOURCE_ROOTS）")
    ws_root = Path(os.path.abspath(workspace))
    scope = f"p{int(project_id)}" if project_id is not None else "shared"
    sandbox = ws_root / "ut_sources" / scope
    try:
        checked_path(ws_root, sandbox, exists=False)
    except EvidenceError as exc:
        raise PermissionDenied("项目源码沙箱路径校验失败") from exc
    sandbox.mkdir(parents=True, exist_ok=True)
    return [sandbox]


def assert_allowed_path(path: str | Path,
                        allowed_roots: Sequence[str | Path],
                        *, exists: bool = False) -> Path:
    """Verify *path* is an absolute, non-reparse path inside *allowed_roots*."""
    raw = str(path or "").strip()
    if not raw or any(ch in raw for ch in ("\x00", "\r", "\n")) or not os.path.isabs(raw):
        raise ServiceError("请输入执行服务器上的绝对路径", code="VALIDATION_ERROR")
    roots = _validated_roots(allowed_roots)
    if not roots:
        raise PermissionDenied("未配置允许访问的执行服务器源码根目录（UT_ALLOWED_SOURCE_ROOTS）")
    target = Path(os.path.abspath(raw))
    for root in roots:
        try:
            return checked_path(root, target, exists=exists)
        except EvidenceError:
            continue
    raise PermissionDenied("路径不在允许的源码根目录（UT_ALLOWED_SOURCE_ROOTS）范围内或包含重解析点")


def _is_root(directory: str, roots: Sequence[Path]) -> bool:
    norm_dir = os.path.normcase(os.path.abspath(directory))
    return any(norm_dir == os.path.normcase(os.path.abspath(root)) for root in roots)


def _parent(directory: str, roots: Sequence[Path]) -> str:
    if _is_root(directory, roots):
        return ""
    parent = os.path.dirname(directory)
    return "" if parent == directory else parent


def _start(path: str, roots: Sequence[Path]) -> tuple[str, str | None]:
    """The directory to list for *path* within *roots*, and any selected file.

    A path that no longer exists opens at its nearest existing ancestor, so a
    stale binding still lands near where it used to point without escaping the
    allowed root boundary.
    """
    assert_allowed_path(path, roots, exists=False)
    current = os.path.abspath(path)
    selected = None
    if not _reparse_component(current) and os.path.isfile(current):
        current, selected = os.path.dirname(current), current
    while not os.path.isdir(current):
        if _is_root(current, roots):
            return "", None
        parent = os.path.dirname(current)
        if parent == current:
            return "", None
        current = parent
    assert_allowed_path(current, roots, exists=True)
    return current, selected


def _root_entries(roots: Sequence[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for root in roots:
        if not _reparse_component(root) and root.is_dir():
            entries.append({
                "name": root.name or str(root),
                "path": str(root),
                "type": "directory",
            })
    return entries


def browse(path: str, kind: str,
           *, allowed_roots: Sequence[str | Path] | None = None,
           project_id: int | None = None) -> dict[str, Any]:
    """List the sub-directories (and, for ``csv``, CSV files) of *path*.

    An empty *path* lists only the configured allowed source roots, never
    arbitrary host drives or ``/``.
    """
    if kind not in KINDS:
        raise ServiceError(f"不支持的浏览类型：{kind}", code="VALIDATION_ERROR")
    path = (path or "").strip()
    if path and not os.path.isabs(path):
        raise ServiceError("请输入执行服务器上的绝对路径", code="VALIDATION_ERROR")
    roots = (_validated_roots(parse_roots(allowed_roots))
             if allowed_roots is not None
             else configured_roots(project_id=project_id))
    if not roots:
        raise PermissionDenied("未配置允许访问的执行服务器源码根目录（UT_ALLOWED_SOURCE_ROOTS）")
    directory, selected = _start(path, roots) if path else ("", None)
    if not directory:
        return {"path": "", "parent": None, "selected": None,
                "entries": _root_entries(roots), "truncated": False}
    entries = []
    try:
        with os.scandir(directory) as items:
            for item in items:
                try:
                    if _reparse_component(item.path):
                        continue
                    if item.is_dir(follow_symlinks=False):
                        entries.append({"name": item.name, "path": item.path, "type": "directory"})
                    elif (kind == "csv" and item.name.lower().endswith(".csv")
                          and item.is_file(follow_symlinks=False)):
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
    return {"path": directory, "parent": _parent(directory, roots), "selected": selected,
            "entries": entries[:_ENTRY_LIMIT], "truncated": len(entries) > _ENTRY_LIMIT}
