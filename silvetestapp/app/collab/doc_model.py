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

from typing import Any

from pycrdt import Array, Doc, Map

from ..models import TestItemRow

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
    can bind to a stable set of top-level keys even before any row is added."""
    with doc.transaction(origin="bootstrap"):
        for sheet in sheets():
            key = sheet_key(sheet)
            if key not in doc:
                doc[key] = Array()


def snapshot_sheet(doc: Doc, sheet: str) -> list[dict[str, Any]]:
    """Return the sheet's rows as plain dicts, in visual (array) order."""
    key = sheet_key(sheet)
    if key not in doc:
        return []
    arr = doc[key]
    return [dict(row) for row in arr.to_py()]


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
    key = sheet_key(sheet)
    if key not in doc:
        return 0
    arr = doc[key]
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
