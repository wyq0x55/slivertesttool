"""REST fields reach live and persisted editors without stale-snapshot loss.

These regressions use PostgreSQL and real pycrdt documents. They exercise the
same item service used by CLI PATCH requests and the actual collab delivery
loop, including persistence failure and later browser edits.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

pytest.importorskip("pycrdt")


@pytest.fixture
def sync_env(app_ctx):
    from app.extensions import db
    from app.models import FieldDefinition, LMUser, Project, ProjectMember, TestItemRow

    with app_ctx.app_context():
        user = LMUser(username="sync-editor", display_name="Sync editor")
        user.set_password("sync-test-password")
        db.session.add(user)
        db.session.flush()
        project = Project(code="SYNC", name="Sync", status="active",
                          owner_id=user.id, created_by=user.id)
        db.session.add(project)
        db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id,
                                     role="project_admin"))
        fields = {"test": "description", "const": "const_value",
                  "lib": "lib_code", "io": "io_path"}
        rows = {}
        for sheet, key in fields.items():
            db.session.add(FieldDefinition(
                project_id=project.id, field_key=key, display_name=key,
                data_type="text", sheet=sheet, is_active=True))
            row = TestItemRow(project_id=project.id, sheet=sheet, uuid=f"sync-{sheet}",
                              case_id=f"TC-{sheet}", title="original title", row_order=1,
                              custom_values={key: "old"}, created_by=user.id)
            db.session.add(row)
            db.session.flush()
            rows[sheet] = row.id
        db.session.add(FieldDefinition(
            project_id=project.id, field_key="remark", display_name="Remark",
            data_type="text", sheet="test", is_active=True))
        db.session.commit()
        ids = (user.id, project.id, rows)
    app_ctx.config.update(COLLAB_WRITEBACK_POLL_SECONDS=3600,
                          COLLAB_PRESENCE_HEARTBEAT_SECONDS=3600)
    return app_ctx, *ids


def _patch(env, changes, sheet="test", *, commit=True, version=None):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import items_service

    app, uid, pid, row_ids = env
    with app.app_context():
        row = db.session.get(TestItemRow, row_ids[sheet])
        result = items_service.update_item(
            db.session.get(LMUser, uid), db.session.get(Project, pid), row,
            row.version if version is None else version, changes, commit=commit)
        if not commit:
            db.session.rollback()
        return result.id


def _stored(env, sheet="test"):
    from app.extensions import db
    from app.models import TestItemRow

    app, _, _, ids = env
    with app.app_context():
        return db.session.get(TestItemRow, ids[sheet]).to_dict()


def _pending(env):
    from app.models import RowWriteback

    app, _, pid, _ = env
    with app.app_context():
        return RowWriteback.query.filter_by(project_id=pid, applied_at=None).count()


def _doc_row(doc, sheet="test"):
    from app.collab import doc_model
    return doc_model.snapshot_sheet(doc, sheet)[0]


@pytest.mark.parametrize("sheet,key", [
    ("test", "description"), ("const", "const_value"),
    ("lib", "lib_code"), ("io", "io_path"),
])
def test_live_patch_survives_stale_flush_then_browser_edit(sync_env, sheet, key):
    from app.collab.server import CollabWebsocketServer
    from pycrdt import Array

    app, _, pid, _ = sync_env

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            assert _doc_row(room.ydoc, sheet)[key] == "old"
            _patch(sync_env, {key: "new from CLI"}, sheet)
            # A browser flush may beat the polling delivery. It cannot revert
            # the DB while the transactional mirror is still pending.
            mat = server._materializers[f"project:{pid}"]
            await mat._flush()
            assert _stored(sync_env, sheet)[key] == "new from CLI"
            await server._drain_writebacks()
            assert _doc_row(room.ydoc, sheet)[key] == "new from CLI"
            assert _pending(sync_env) == 0
            # Once delivered, subsequent browser edits must work normally.
            room.ydoc.get(f"rows:{sheet}", type=Array)[0][key] = "later browser edit"
            await mat._flush()
            assert _stored(sync_env, sheet)[key] == "later browser edit"

    asyncio.run(scenario())


def test_inactive_persisted_room_replays_old_pending_patch_before_open(sync_env):
    from app.collab import doc_model
    from app.collab.pg_ystore import PgYStore
    from app.collab.server import CollabWebsocketServer
    from app.extensions import db
    from app.models import RowWriteback
    from pycrdt import Doc

    app, _, pid, _ = sync_env
    stale = Doc()
    with app.app_context():
        doc_model.bootstrap_doc(stale, pid)
    store = PgYStore(f"project:{pid}", app)
    store._write_sync(stale.get_update(), None, 1.0)
    _patch(sync_env, {"description": "offline CLI change"})
    with app.app_context():
        RowWriteback.query.filter_by(project_id=pid).update(
            {RowWriteback.created_at: datetime.utcnow() - timedelta(days=2)})
        db.session.commit()

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            assert _doc_row(room.ydoc)["description"] == "offline CLI change"
            assert _pending(sync_env) == 0
        # Rehydrate persisted updates independently, as after process restart.
        rebuilt = Doc()
        async for update, _, _ in PgYStore(f"project:{pid}", app).read():
            rebuilt.apply_update(update)
        assert _doc_row(rebuilt)["description"] == "offline CLI change"

    asyncio.run(scenario())


def test_failed_persistence_leaves_message_pending_for_retry(sync_env, monkeypatch):
    from app.collab.server import CollabWebsocketServer

    app, _, pid, _ = sync_env

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            _patch(sync_env, {"description": "must survive persistence failure"})
            async def fail_write(*args, **kwargs):
                raise RuntimeError("injected YStore failure")

            with monkeypatch.context() as patcher:
                patcher.setattr(room.ystore, "write", fail_write)
                with pytest.raises(RuntimeError, match="injected YStore failure"):
                    await server._drain_writebacks()
            assert _pending(sync_env) == 1
            await server._drain_writebacks()
            assert _pending(sync_env) == 0
            assert _doc_row(room.ydoc)["description"] == "must survive persistence failure"

    asyncio.run(scenario())


def test_patch_rollback_and_version_conflict_do_not_publish(sync_env):
    from app.services.lanmatrix.errors import VersionConflict

    _patch(sync_env, {"description": "rolled back"}, commit=False)
    assert _stored(sync_env)["description"] == "old"
    assert _pending(sync_env) == 0
    _patch(sync_env, {"description": "accepted"})
    with pytest.raises(VersionConflict):
        _patch(sync_env, {"description": "stale writer"}, version=1)
    assert _stored(sync_env)["description"] == "accepted"
    assert _pending(sync_env) == 1


def test_rest_patch_allows_live_collaboration_and_preserves_other_guards(sync_env, monkeypatch):
    from app.collab import presence

    app, _, pid, rows = sync_env
    monkeypatch.setattr(presence, "is_collab_active", lambda _: True)
    client = app.test_client()
    login = client.post("/api/v1/auth/login", json={
        "username": "sync-editor", "password": "sync-test-password"})
    assert login.status_code == 200
    headers = {"X-CSRF-Token": login.get_json()["data"]["csrf_token"]}
    url = f"/api/v1/projects/{pid}/items/{rows['test']}"
    response = client.patch(url, json={"version": 1, "changes": {
        "description": "REST live change", "remark": "ID;;exampleでテストされています。"}},
                            headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored(sync_env)["description"] == "REST live change"
    assert _pending(sync_env) == 1
    response = client.delete(url, headers=headers)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "COLLAB_ACTIVE"
    unauthenticated = app.test_client().patch(url, json={"version": 2, "changes": {
        "description": "unauthorized"}})
    assert unauthenticated.status_code in (401, 403)
    assert _stored(sync_env)["description"] == "REST live change"


def test_retry_after_ack_failure_keeps_later_browser_edit(sync_env, monkeypatch):
    from app.collab import writeback
    from app.collab.server import CollabWebsocketServer
    from pycrdt import Array

    app, _, pid, _ = sync_env

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            _patch(sync_env, {"description": "accepted before failed ack"})

            def fail_ack(*args, **kwargs):
                raise RuntimeError("injected acknowledgement failure")

            with monkeypatch.context() as patcher:
                patcher.setattr(writeback, "acknowledge", fail_ack)
                with pytest.raises(RuntimeError, match="injected acknowledgement failure"):
                    await server._drain_writebacks()
            assert _pending(sync_env) == 1
            room.ydoc.get("rows:test", type=Array)[0]["description"] = "browser after delivery"
            await server._drain_writebacks()
            assert _pending(sync_env) == 0
            assert _doc_row(room.ydoc)["description"] == "browser after delivery"
            await server._materializers[f"project:{pid}"]._flush()
            assert _stored(sync_env)["description"] == "browser after delivery"

    asyncio.run(scenario())


def test_noop_patch_repairs_existing_stale_document(sync_env):
    from app.collab import doc_model
    from app.collab.pg_ystore import PgYStore
    from app.collab.server import CollabWebsocketServer
    from pycrdt import Array, Doc

    app, _, pid, _ = sync_env
    stale = Doc()
    with app.app_context():
        doc_model.bootstrap_doc(stale, pid)
    stale.get("rows:test", type=Array)[0]["description"] = "historical stale cell"
    PgYStore(f"project:{pid}", app)._write_sync(stale.get_update(), None, 1.0)
    _patch(sync_env, {"description": "old"})

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            assert _doc_row(room.ydoc)["description"] == "old"
            assert _pending(sync_env) == 0

    asyncio.run(scenario())


def test_missing_live_target_is_restored_before_ack(sync_env):
    from app.collab import doc_model
    from app.collab.pg_ystore import PgYStore
    from app.collab.server import CollabWebsocketServer
    from pycrdt import Array, Doc

    app, _, pid, _ = sync_env
    stale = Doc()
    with app.app_context():
        doc_model.bootstrap_doc(stale, pid)
    del stale.get("rows:test", type=Array)[0]
    PgYStore(f"project:{pid}", app)._write_sync(stale.get_update(), None, 1.0)
    _patch(sync_env, {"description": "target omitted from historical document"})

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            assert _doc_row(room.ydoc)["description"] == "target omitted from historical document"
            assert _pending(sync_env) == 0
            await server._materializers[f"project:{pid}"]._flush()
            assert _stored(sync_env)["description"] == "target omitted from historical document"

    asyncio.run(scenario())


def test_aliases_typed_lists_and_step_json_keep_their_contract(sync_env):
    import json
    from app.collab.server import CollabWebsocketServer
    from app.extensions import db
    from app.models import FieldDefinition

    app, _, pid, _ = sync_env
    with app.app_context():
        for sheet, key, kind in [("test", "tags", "multi_select"),
                                 ("test", "steps", "steps"),
                                 ("lib", "lib_stb", "steps")]:
            db.session.add(FieldDefinition(
                project_id=pid, field_key=key, display_name=key,
                data_type=kind, sheet=sheet, is_active=True))
        db.session.commit()
    procedure = json.dumps({"input_signals": [], "expected_signals": [],
                            "steps": [{"description": "procedure from CLI"}]})

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            _patch(sync_env, {"tags": ["one", "two"], "remark": "updated alias",
                              "steps": procedure})
            _patch(sync_env, {"lib_stb": procedure}, "lib")
            await server._drain_writebacks()
            row = _doc_row(room.ydoc)
            assert row["tags"] == ["one", "two"]
            assert row["remark"] == row["comment"] == "updated alias"
            assert json.loads(row["steps"]) == json.loads(procedure)
            assert json.loads(_doc_row(room.ydoc, "lib")["lib_stb"]) == json.loads(procedure)
            await server._materializers[f"project:{pid}"]._flush()
            assert _stored(sync_env)["tags"] == ["one", "two"]
            assert _stored(sync_env)["remark"] == "updated alias"

    asyncio.run(scenario())


def test_later_deletion_supersedes_pending_patch_in_persisted_document(sync_env):
    from app.collab import doc_model
    from app.collab.pg_ystore import PgYStore
    from app.collab.server import CollabWebsocketServer
    from app.extensions import db
    from app.models import TestItemRow
    from pycrdt import Doc

    app, _, pid, rows = sync_env
    stale = Doc()
    with app.app_context():
        doc_model.bootstrap_doc(stale, pid)
    PgYStore(f"project:{pid}", app)._write_sync(stale.get_update(), None, 1.0)
    _patch(sync_env, {"description": "accepted before later deletion"})
    with app.app_context():
        db.session.get(TestItemRow, rows["test"]).deleted_at = datetime.utcnow()
        db.session.commit()

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            assert doc_model.snapshot_sheet(room.ydoc, "test") == []
            await server._materializers[f"project:{pid}"]._flush()
            with app.app_context():
                assert db.session.get(TestItemRow, rows["test"]).deleted_at is not None
            assert _pending(sync_env) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("flush_before_delivery", [False, True])
def test_live_browser_deletion_wins_over_pending_patch(sync_env, flush_before_delivery):
    from app.collab import doc_model
    from app.collab.server import CollabWebsocketServer
    from app.extensions import db
    from app.models import TestItemRow
    from pycrdt import Array

    app, _, pid, rows = sync_env

    async def scenario():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            _patch(sync_env, {"description": "pending while browser deletes"})
            del room.ydoc.get("rows:test", type=Array)[0]
            mat = server._materializers[f"project:{pid}"]
            if flush_before_delivery:
                await mat._flush()
            await server._drain_writebacks()
            await mat._flush()
            assert doc_model.snapshot_sheet(room.ydoc, "test") == []
            with app.app_context():
                assert db.session.get(TestItemRow, rows["test"]).deleted_at is not None
            assert _pending(sync_env) == 0

    asyncio.run(scenario())
