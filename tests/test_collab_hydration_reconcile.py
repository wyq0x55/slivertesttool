"""A room opened from persisted updates catches up with offline REST writes.

While no room is live, REST and the CLI write the database only. These
regressions use PostgreSQL and real pycrdt documents to check that the next
room open folds those writes into the Doc instead of reverting them, and that
a Doc with edits that were never materialized is not overwritten.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pycrdt")

from tests.test_collab_rest_sync import _patch, _pending, sync_env  # noqa: E402,F401


def _room_name(pid):
    return f"project:{pid}"


def _session(app, pid, body=None):
    """Open the room in a fresh server, run ``body``, flush, shut down."""
    from app.collab.server import CollabWebsocketServer

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(_room_name(pid))
            result = body(room.ydoc) if body else None
            await server._materializers[_room_name(pid)]._flush()
            return result

    return asyncio.run(scenario())


def _doc_uuids(doc, sheet="test"):
    from app.collab import doc_model
    return [row["uuid"] for row in doc_model.snapshot_sheet(doc, sheet)]


def _add_row(env, case_id, **values):
    from app.extensions import db
    from app.models import LMUser, Project
    from app.services.lanmatrix import items_service

    app, uid, pid, _ = env
    with app.app_context():
        row = items_service.create_item(
            db.session.get(LMUser, uid), db.session.get(Project, pid),
            {"case_id": case_id, "title": case_id, **values})
        return row.id, row.uuid


def _row(app, row_id):
    from app.extensions import db
    from app.models import TestItemRow
    with app.app_context():
        row = db.session.get(TestItemRow, row_id)
        return row.to_dict(), row.deleted_at


def test_row_created_offline_appears_and_survives_the_next_flush(sync_env):
    app, _, pid, _ = sync_env
    _session(app, pid)                       # editor used once, then closed
    new_id, new_uuid = _add_row(sync_env, "TC-offline")

    def edit(doc):
        from pycrdt import Array
        assert _doc_uuids(doc) == ["sync-test", new_uuid]
        doc.get("rows:const", type=Array)[0]["title"] = "browser edit"

    _session(app, pid, edit)
    state, deleted_at = _row(app, new_id)
    assert deleted_at is None and state["case_id"] == "TC-offline"


def test_row_deleted_offline_is_not_resurrected(sync_env):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import items_service

    app, uid, pid, _ = sync_env
    gone_id, gone_uuid = _add_row(sync_env, "TC-gone")
    _session(app, pid)
    with app.app_context():
        items_service.soft_delete_item(db.session.get(LMUser, uid),
                                       db.session.get(Project, pid),
                                       db.session.get(TestItemRow, gone_id))

    def edit(doc):
        from pycrdt import Array
        assert _doc_uuids(doc) == ["sync-test"]
        doc.get("rows:test", type=Array)[0]["title"] = "browser edit"

    _session(app, pid, edit)
    assert _row(app, gone_id)[1] is not None


def test_offline_field_edit_and_reorder_reach_the_document(sync_env):
    from app.collab import doc_model
    from app.extensions import db
    from app.models import TestItemRow

    app, _, pid, ids = sync_env
    second_id, second_uuid = _add_row(sync_env, "TC-2")
    third_id, third_uuid = _add_row(sync_env, "TC-3")
    _session(app, pid)
    _patch(sync_env, {"description": "offline CLI change"})
    with app.app_context():                  # offline move: TC-3 to the top
        db.session.get(TestItemRow, third_id).row_order = 0
        db.session.commit()

    def check(doc):
        assert _doc_uuids(doc) == [third_uuid, "sync-test", second_uuid]
        rows = {row["uuid"]: row for row in doc_model.snapshot_sheet(doc, "test")}
        assert rows["sync-test"]["description"] == "offline CLI change"

    _session(app, pid, check)
    assert _pending(sync_env) == 0
    assert _row(app, ids["test"])[0]["description"] == "offline CLI change"
    assert _row(app, third_id)[0]["row_order"] < _row(app, ids["test"])[0]["row_order"]


def test_unmaterialized_browser_edit_survives_a_crash(sync_env):
    """The Doc is ahead of its sync mark: keep it, except REST-versioned rows."""
    from app.collab import doc_model
    from app.collab.pg_ystore import PgYStore
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import items_service
    from pycrdt import Array, Doc

    app, uid, pid, ids = sync_env
    other_id, other_uuid = _add_row(sync_env, "TC-other")
    _session(app, pid)

    async def crash_before_flush():
        # A browser edit reached the store, but the process died before the
        # materializer wrote it to the database.
        doc, store = Doc(), PgYStore(_room_name(pid), app)
        async for update, _, _ in store.read():
            doc.apply_update(update)
        before = doc.get_state()
        rows = doc.get("rows:test", type=Array)
        next(row for row in rows if row["uuid"] == "sync-test")["description"] = \
            "unflushed edit"
        await store.write(doc.get_update(before))

    asyncio.run(crash_before_flush())
    with app.app_context():                  # CLI PATCH on another row
        row = db.session.get(TestItemRow, other_id)
        items_service.update_item(db.session.get(LMUser, uid),
                                  db.session.get(Project, pid), row,
                                  row.version, {"remark": "CLI remark"})

    def check(doc):
        rows = {row["uuid"]: row for row in doc_model.snapshot_sheet(doc, "test")}
        assert rows["sync-test"]["description"] == "unflushed edit"
        assert rows[other_uuid]["remark"] == "CLI remark"

    _session(app, pid, check)
    assert _row(app, ids["test"])[0]["description"] == "unflushed edit"
    assert _row(app, other_id)[0]["remark"] == "CLI remark"
    assert _pending(sync_env) == 0


def test_outbox_of_projects_without_a_room_is_acknowledged(sync_env):
    from app.collab import writeback

    app, _, pid, _ = sync_env
    _patch(sync_env, {"description": "nobody is editing"})
    assert _pending(sync_env) == 1
    with app.app_context():
        assert writeback.acknowledge_offline([pid]) == 0     # live: keep
        assert writeback.acknowledge_offline([]) == 1
    assert _pending(sync_env) == 0


def test_rows_the_database_never_accepted_keep_their_in_progress_cells():
    """Pure Doc check: no database involved."""
    from app.collab import doc_model
    from pycrdt import Array, Doc, Map

    doc = Doc()
    doc_model.ensure_sheets(doc)
    arr = doc.get("rows:test", type=Array)
    with doc.transaction():
        arr.append(Map({"uuid": "draft", "id": -1, "title": "never saved"}))
        arr.append(Map({"uuid": "b", "id": 2, "version": 1, "priority": "bad"}))
        arr.append(Map({"uuid": "a", "id": 1, "version": 1, "priority": "bad"}))
        doc.get(doc_model.ERRORS_KEY, type=Map)["a"] = "{}"
        doc.get(doc_model.ERRORS_KEY, type=Map)["b"] = "{}"
    states = [{"uuid": "a", "id": 1, "version": 1, "priority": "P1"},
              {"uuid": "b", "id": 2, "version": 2, "priority": "P2"}]
    with doc.transaction():
        doc_model.reconcile_from_db(doc, "test", states, full=True)

    rows = doc_model.snapshot_sheet(doc, "test")
    assert [row["uuid"] for row in rows] == ["draft", "a", "b"]
    by_uuid = {row["uuid"]: row for row in rows}
    assert by_uuid["a"]["priority"] == "bad"      # rejected, database unchanged
    assert by_uuid["b"]["priority"] == "P2"       # database changed offline
