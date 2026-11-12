"""The production materializer serializes snapshots with durable delivery.

Database races are covered by test_collab_rest_sync_races. These tests drive
Materializer._flush itself to establish the room-level lock ordering.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("pycrdt")


def test_delivery_waits_until_existing_snapshot_has_committed(monkeypatch):
    from app.collab import doc_model
    from app.collab.materializer import Materializer
    from pycrdt import Array, Doc, Map

    async def scenario():
        doc = Doc()
        doc_model.ensure_sheets(doc)
        doc.get("rows:test", type=Array).append(Map({
            "uuid": "row-1", "description": "old"}))
        mat = Materializer(1, None, debounce=3600)
        mat.attach(doc)
        snapshot_started, allow_commit = threading.Event(), threading.Event()
        committed = []

        def materialize(snapshot, row_uid):
            snapshot_started.set()
            assert allow_commit.wait(5)
            committed.append(snapshot["test"][0]["description"])
            return {}, {}, {}

        monkeypatch.setattr(mat, "_materialize_sync", materialize)
        flushing = asyncio.create_task(mat._flush())
        for _ in range(500):
            if snapshot_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert snapshot_started.is_set()
        delivered = asyncio.Event()

        async def deliver():
            async with mat.write_lock:
                with mat.suppressed():
                    doc.get("rows:test", type=Array)[0]["description"] = "new"
                delivered.set()

        delivery = asyncio.create_task(deliver())
        await asyncio.sleep(0)
        assert not delivered.is_set(), "delivery cannot acknowledge an in-flight stale snapshot"
        allow_commit.set()
        await flushing
        await delivery
        assert committed == ["old"]
        await mat._flush()
        assert committed == ["old", "new"]
        mat.detach()

    asyncio.run(scenario())


def test_schedule_flush_defers_while_a_flush_is_in_flight():
    from app.collab.materializer import Materializer

    mat = Materializer(project_id=1, flask_app=None)
    mat._flushing = True
    mat.schedule_flush()
    assert mat._dirty_again is True
