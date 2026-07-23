"""Mapping between the per-project ``Y.Doc`` and ``TestItemRow`` rows.

CRDT shape (design doc §3.1): the project ``Doc`` holds one ``Y.Array`` per
editor sheet, keyed ``rows:{sheet}`` (``rows:test`` / ``rows:const`` /
``rows:lib``). Each array element is a ``Y.Map`` whose keys are the row's field
values plus the stable ``uuid`` used as the CRDT row identity.

* :func:`bootstrap_doc` seeds an empty ``Doc`` from the current DB state the
  first time a room is opened (when the YStore has no persisted updates).
* :func:`snapshot_sheet` reads a sheet's array back into the plain list of row
  dicts that :func:`items_service.materialize_sheet` consumes.

``bootstrap_doc`` must run inside a Flask app context (it touches ``db``); the
pure ``Y`` object construction does not.
"""

from __future__ import annotations

import json
from typing import Any

from pycrdt import Array, Doc, Map

from ..models import TestItemRow

# Top-level Y.Map key holding the authoritative row-validation errors. The
# server is the single writer of this channel; clients only observe it to paint
# offending cells red (design §12.2). Keyed by row ``uuid``.
ERRORS_KEY = "row_errors"

# Field keys we never seed into the Y.Map: server-only bookkeeping that clients
# don't edit and that materialization recomputes. ``uuid`` and ``id`` ARE kept
# (clients need them for cursors / linking), but ``row_order``/timestamps are
# implicit in array position / server clock.
_SEED_SKIP = {"row_order", "updated_at"}


def sheet_key(sheet: str) -> str:
    from ..services.lanmatrix import fields as fld
    return fld.sheet_row_key(sheet)


def sheets() -> list[str]:
    from ..services.lanmatrix import fields as fld
    return list(fld.SHEETS)


def _row_state(item: TestItemRow) -> dict[str, Any]:
    state = item.to_dict()
    for k in _SEED_SKIP:
        state.pop(k, None)
    return state


def bootstrap_doc(doc: Doc, project_id: int) -> int:
    """Populate an empty ``doc`` from the live DB rows of ``project_id``.

    Returns the number of rows seeded. Call only when the YStore had nothing to
    replay (a brand-new room); otherwise the persisted CRDT history is authority
    and re-seeding would duplicate rows.
    """
    seeded = 0
    with doc.transaction(origin="bootstrap"):
        for sheet in sheets():
            arr = Array()
            doc[sheet_key(sheet)] = arr
            rows = (TestItemRow.query
                    .filter_by(project_id=project_id, sheet=sheet, deleted_at=None)
                    .order_by(TestItemRow.row_order.asc()).all())
            for item in rows:
                arr.append(Map(_row_state(item)))
                seeded += 1
    return seeded


def ensure_sheets(doc: Doc) -> None:
    """Make sure every sheet array exists (empty is fine) so observers/clients
    can bind to a stable set of top-level keys even before any row is added.

    Uses the typed ``doc.get(key, type=Array)`` accessor rather than a
    ``key not in doc`` guard: on a room hydrated from persisted updates the root
    already exists at the CRDT level (so ``key in doc`` is true) but has never
    been bound to a Python ``Array`` handle, leaving ``doc[key]`` returning
    ``None``. ``get`` both creates a missing root AND binds an existing one, so
    every later ``snapshot_sheet`` sees a real typed array.
    """
    for sheet in sheets():
        doc.get(sheet_key(sheet), type=Array)
    # Bind the shared error channel too, so clients can observe it from the
    # first sync even before any validation error exists.
    doc.get(ERRORS_KEY, type=Map)


def _steps_field(sheet: str) -> str | None:
    from ..services.lanmatrix import fields as fld
    return fld.SHEET_STEPS_FIELD.get(sheet)


def snapshot_sheet(doc: Doc, sheet: str) -> list[dict[str, Any]]:
    """Return the sheet's rows as plain dicts, in visual (array) order.

    The step-detail field ("steps"/"lib_stb") may be a nested CRDT sub-structure
    (item 3): clients upgrade the legacy JSON *string* into a nested ``Y.Map`` for
    granular collaborative editing. ``to_py()`` returns that as a nested Python
    dict, but the whole downstream pipeline — materialize -> DB ``steps`` column
    -> execution-JSON export -> Excel import/export — treats this field as an
    opaque JSON string. So we re-serialise a nested value back to a string here,
    keeping that contract intact and the server ignorant of the sub-structure.
    """
    arr = doc.get(sheet_key(sheet), type=Array)
    rows = [dict(row) for row in arr.to_py()]
    field = _steps_field(sheet)
    if field:
        for row in rows:
            val = row.get(field)
            if isinstance(val, (dict, list)):
                try:
                    row[field] = json.dumps(val, ensure_ascii=False)
                except (TypeError, ValueError):
                    pass
    return rows


def write_row_errors(doc: Doc, errors_by_uuid: dict[str, Any]) -> int:
    """Publish the authoritative per-row validation errors into the Y.Doc.

    ``errors_by_uuid`` maps ``uuid -> {"cells": [...], "message": str}`` for the
    rows that failed this reconcile. The whole channel is rebuilt as a snapshot
    (the server is the single writer), so a row that was fixed since the last
    flush has its entry removed automatically. Only genuinely changed entries are
    touched, to avoid needless observer churn. Returns the number of rows
    currently in error.

    Each value is stored as a **JSON string** (a primitive), not a nested dict:
    pycrdt would otherwise convert a nested dict into a nested ``Y.Map`` and the
    yjs client would read a Y type instead of a plain object. A JSON string is
    unambiguous across both runtimes; the client ``JSON.parse``s it.

    MUST be called inside a ``doc.transaction()`` **and** the materializer's
    ``suppressed()`` block so the write does not re-trigger a reconcile.
    """
    emap = doc.get(ERRORS_KEY, type=Map)
    desired = {u: json.dumps(info, ensure_ascii=False, sort_keys=True)
               for u, info in (errors_by_uuid or {}).items()}
    for row_uuid in list(emap.keys()):
        if row_uuid not in desired:
            del emap[row_uuid]
    for row_uuid, payload in desired.items():
        if emap.get(row_uuid) != payload:
            emap[row_uuid] = payload
    return len(desired)


def write_back_ids(doc: Doc, sheet: str,
                   id_map: dict[str, tuple[int, int]]) -> int:
    """Write authoritative server ``id`` / ``version`` back onto matching rows.

    ``id_map`` maps ``uuid -> (id, version)``. For every ``Y.Map`` in the sheet
    array whose ``uuid`` is present, set its ``id`` and ``version`` when they
    differ (a client-created row starts with a temporary negative ``id`` until
    the server materializes it and assigns the real primary key). Returns the
    number of rows changed.

    MUST be called inside a ``doc.transaction()`` **and** the materializer's
    ``suppressed()`` block so the write does not re-trigger a reconcile.
    """
    arr = doc.get(sheet_key(sheet), type=Array)
    rows = arr.to_py()  # plain dicts, cheap to scan for uuid + index
    changed = 0
    for index, row in enumerate(rows):
        row_uuid = (row or {}).get("uuid")
        target = id_map.get(row_uuid) if row_uuid else None
        if not target:
            continue
        new_id, new_ver = target
        ymap = arr[index]
        touched = False
        if row.get("id") != new_id:
            ymap["id"] = new_id
            touched = True
        if row.get("version") != new_ver:
            ymap["version"] = new_ver
            touched = True
        if touched:
            changed += 1
    return changed
