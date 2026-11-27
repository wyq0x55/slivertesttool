"""Silver workbook downloads wait for durable, materialized browser edits."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from tests.test_silver_books import _import_book, _item, _setup, _steps


@pytest.fixture
def books(app_ctx, tmp_path):
    with app_ctx.app_context():
        user, project, model = _setup(app_ctx, tmp_path)
        book_ids = [
            _import_book(user, project, model, filename=f"{name}.xlsx", book_name=name,
                         items=[_item("QTS-001", name)])["book_id"]
            for name in ("First", "Second")
        ]
        client = app_ctx.test_client()
        with client.session_transaction() as session:
            session["lm_user_id"] = user.id
        return client, project.id, book_ids


def _urls(project_id, book_id):
    base = f"/api/v1/projects/{project_id}/books"
    return f"{base}/{book_id}/export", f"{base}/export.zip"


def _assert_pending(client, urls, **kwargs):
    for url in urls:
        response = client.get(url, **kwargs)
        assert response.status_code == 409
        assert response.json["error"]["code"] == "COLLAB_BOOK_EXPORT_PENDING"


def _purposes(data):
    from app.services.lanmatrix import book_excel

    return [row["steps"]["steps"][0]["purpose"]
            for row in book_excel.parse_workbook(io.BytesIO(data))["items"]]


async def _flush(materializer):
    if materializer._timer is not None:
        materializer._timer.cancel()
        materializer._timer = None
    await materializer._flush()


def test_exports_guard_browser_edits_until_the_database_is_current(app_ctx, books):
    from pycrdt import Array, Doc, Map, create_update_message, handle_sync_message

    from app.collab.book_export import FENCE_KEY
    from app.collab.server import CollabWebsocketServer

    client, project_id, (first, _second) = books
    current_url, zip_url = urls = _urls(project_id, first)
    headers = {"X-LM-Export-Fence": "download-request"}

    async def scenario():
        async with CollabWebsocketServer(app_ctx) as server:
            name = f"project:{project_id}"
            room = await server.get_room(name)
            assert _purposes(client.get(current_url).data) == ["First"]

            browser = Doc()
            browser.apply_update(room.ydoc.get_update())
            before = browser.get_state()
            rows = browser.get("rows:test", type=Array)
            row = next(row for row in rows if row["book_id"] == first)
            row["steps"] = Map(_steps("just edited"))
            browser.get(FENCE_KEY, type=Map)[headers["X-LM-Export-Fence"]] = True
            message = create_update_message(browser.get_update(before))

            _assert_pending(client, urls, headers=headers)
            assert await room.on_message(message) is False
            _assert_pending(client, urls)
            handle_sync_message(message[1:], room.ydoc)
            _assert_pending(client, urls, headers=headers)

            await _flush(server._materializers[name])
            current = client.get(current_url, headers=headers)
            assert current.status_code == 200
            assert "First.xlsx" in current.headers["Content-Disposition"]
            assert _purposes(current.data) == ["just edited"]

            archive = client.get(zip_url, headers=headers)
            assert archive.status_code == 200
            with zipfile.ZipFile(io.BytesIO(archive.data)) as zipped:
                assert zipped.namelist() == ["First.xlsx", "Second.xlsx"]
                assert _purposes(zipped.read("First.xlsx")) == ["just edited"]
                assert _purposes(zipped.read("Second.xlsx")) == ["Second"]

    asyncio.run(scenario())

    from app.extensions import db
    from app.models import TestItemRow

    with app_ctx.app_context():
        stored = TestItemRow.query.filter_by(project_id=project_id, book_id=first, sheet="test").one()
        stored.set_field("steps", _steps("offline update"))
        db.session.commit()
    assert _purposes(client.get(current_url).data) == ["offline update"]


def test_persisted_delete_blocks_exports_after_room_shutdown(app_ctx, books):
    from pycrdt import Array, Doc, create_update_message

    from app.collab.server import CollabWebsocketServer

    client, project_id, (first, _second) = books
    urls = _urls(project_id, first)
    name = f"project:{project_id}"

    async def scenario():
        async with CollabWebsocketServer(app_ctx) as server:
            room = await server.get_room(name)
            browser = Doc()
            browser.apply_update(room.ydoc.get_update())
            before = browser.get_state()
            rows = browser.get("rows:test", type=Array)
            del rows[next(index for index, row in enumerate(rows) if row["book_id"] == first)]
            assert before == browser.get_state()
            await room.on_message(create_update_message(browser.get_update(before)))
            # A crash after durable ingress, before the room accepted the update.
            server._materializers[name].detach()
        _assert_pending(client, urls)

        async with CollabWebsocketServer(app_ctx) as resumed:
            await resumed.get_room(name)
            await _flush(resumed._materializers[name])
            response = client.get(urls[0])
            assert response.status_code == 200
            assert _purposes(response.data) == []

    asyncio.run(scenario())


def test_intermediate_materialization_invalidates_the_previous_mark(app_ctx, books):
    from app.collab import doc_model
    from app.collab.server import CollabWebsocketServer

    client, project_id, (first, _second) = books
    urls = _urls(project_id, first)
    name = f"project:{project_id}"

    async def scenario():
        async with CollabWebsocketServer(app_ctx) as server:
            room = await server.get_room(name)
            materializer = server._materializers[name]
            snapshot = {sheet: doc_model.snapshot_sheet(room.ydoc, sheet)
                        for sheet in doc_model.sheets()}
            snapshot["test"][0]["steps"] = _steps("intermediate")
            # Simulate the live Doc already back at its prior content when a
            # previously captured intermediate snapshot commits to the rows.
            materializer._materialize_sync(snapshot)
            _assert_pending(client, urls)
            await _flush(materializer)
            response = client.get(urls[0])
            assert response.status_code == 200
            assert _purposes(response.data) == ["First"]

    asyncio.run(scenario())
