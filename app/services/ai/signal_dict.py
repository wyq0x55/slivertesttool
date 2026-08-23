"""Project signal dictionary — the curated source for the semantic registry.

Everything here is deliberately thin: list, replace. The dictionary is small
(a project carries dozens of signals, not thousands), edited rarely, and read
once per generation, so bulk-replace semantics keep the UI and the API a
single textarea round-trip instead of per-row CRUD.
"""

from __future__ import annotations

from typing import Any

from ...extensions import db
from ...models import AiSignalDict


def entries_for(project_id: int) -> list[list[str]]:
    """``[[表示名, 路径, 类型], ...]`` in the pair shape ``registry.build``
    expects for its highest-priority source."""
    rows = (AiSignalDict.query.filter_by(project_id=project_id)
            .order_by(AiSignalDict.path).all())
    return [[r.display or r.path, r.path, r.type or ""] for r in rows]


def replace_entries(project_id: int, editor_id: int,
                    entries: list[Any]) -> int:
    """Replace the project's dictionary wholesale.

    ``entries``: ``[[表示名, 路径, 类型?], ...]`` (path alone is rejected —
    a dictionary row without a display name adds nothing the automatic
    sources don't already provide). Returns the stored row count.
    """
    clean: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    problems: list[str] = []
    for i, entry in enumerate(entries or []):
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            problems.append(f"entries[{i}] 必须是 [表示名, 路径, 类型?] 数组")
            continue
        display = str(entry[0] or "").strip()
        path = str(entry[1] or "").strip()
        type_ = str(entry[2] or "").strip() if len(entry) > 2 else ""
        if not path or not display:
            problems.append(f"entries[{i}] 的表示名和路径都不能为空")
            continue
        if path in seen:
            problems.append(f"entries[{i}] 路径重复：{path}")
            continue
        seen.add(path)
        clean.append((display, path, type_))
    if problems:
        raise ValueError("；".join(problems))

    AiSignalDict.query.filter_by(project_id=project_id).delete(
        synchronize_session=False)
    for display, path, type_ in clean:
        db.session.add(AiSignalDict(
            project_id=project_id, path=path, display=display,
            type=type_, updated_by=editor_id))
    db.session.commit()
    return len(clean)
