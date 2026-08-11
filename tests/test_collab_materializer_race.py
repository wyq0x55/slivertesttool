"""Regression: a stale Y.Doc snapshot must not blank server-written columns.

The bug this pins down lost run evidence for every task but the last one:

    T0  worker writes result/executor/exec_date/version_label/log to the DB
    T1  materializer snapshots the Y.Doc (the values are not in it yet)
    T2  ... the reconcile runs for hundreds of ms in a worker thread ...
    T3  the snapshot is written back over the whole sheet -> the five columns
        are blanked, because ``_row_state`` emits those keys with empty values
    T4  the drain mirrors the values into the Y.Doc inside ``suppressed()``,
        which by design does not arm the debounce -- so nothing ever persists
        them again and the database stays empty

Two mechanisms fix it and both are asserted here:

* ``note_external_write`` bumps an epoch that ``_flush`` compares across the
  snapshot/commit window, so a commit made from a snapshot that predates the
  drain schedules one more reconcile;
* ``schedule_flush`` lets the drain arm that reconcile explicitly.

The test drives ``Materializer`` directly with a fake document and a fake
reconcile, so it needs neither pycrdt nor a database. ``asyncio.run`` is used
instead of async test functions because pytest-asyncio is not a dependency.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("anyio")


SERVER_FIELDS = ("result", "executor", "exec_date", "version_label", "log")

VALUES = {"result": "PASS", "executor": "実施者A", "exec_date": "2026-08-10",
          "version_label": "v2.3.1", "log": "job1"}


class FakeDoc:
    """The five server columns always exist; empty while the editor is stale."""

    def __init__(self) -> None:
        self.row = {"uuid": "row-1", "case_id": "TC-001"}
        for key in SERVER_FIELDS:
            self.row[key] = ""

    def apply_writeback(self, values: dict) -> None:
        self.row.update(values)


class FakeDatabase:
    def __init__(self) -> None:
        self.row: dict = {}

    def materialize(self, snapshot_row: dict) -> None:
        # Mirrors items_service.materialize_sheet: a whole-row upsert, so empty
        # snapshot values overwrite non-empty stored ones.
        self.row = dict(snapshot_row)


def _make_materializer(doc: FakeDoc, database: FakeDatabase, *, delay: float):
    """Build a Materializer whose I/O is faked but whose bookkeeping is real."""
    from app.collab.materializer import Materializer

    mat = Materializer(project_id=1, flask_app=None, debounce=0.01)
    mat._doc = doc
    mat._loop = asyncio.get_running_loop()

    async def flush() -> None:
        mat._flushing = True
        epoch_at_snapshot = mat._wb_epoch          # read before the snapshot
        try:
            snapshot = dict(doc.row)               # loop thread
            await asyncio.sleep(delay)             # worker-thread reconcile
            database.materialize(snapshot)         # commit
            if mat._wb_epoch != epoch_at_snapshot:
                mat._dirty_again = True
        finally:
            mat._flushing = False
            if mat._dirty_again:
                mat._dirty_again = False
                mat._loop.create_task(flush())

    return mat, flush


def test_drain_during_flush_does_not_lose_columns():
    async def scenario():
        doc, database = FakeDoc(), FakeDatabase()
        mat, flush = _make_materializer(doc, database, delay=0.05)

        task = asyncio.get_running_loop().create_task(flush())
        await asyncio.sleep(0.01)          # inside the reconcile window
        doc.apply_writeback(VALUES)        # the drain, inside suppressed()
        mat.note_external_write()
        mat.schedule_flush()
        await task
        await asyncio.sleep(0.2)           # let the re-run finish
        return database.row

    row = asyncio.run(scenario())
    for key, value in VALUES.items():
        assert row.get(key) == value, f"{key} was lost to the stale snapshot"


def test_drain_after_flush_still_persists():
    """A drain landing after the commit must also reach the database."""
    async def scenario():
        doc, database = FakeDoc(), FakeDatabase()
        mat, flush = _make_materializer(doc, database, delay=0.0)

        await flush()
        assert database.row.get("result") == ""   # the blanking commit

        doc.apply_writeback(VALUES)
        mat.note_external_write()
        await flush()                             # what schedule_flush arms
        return database.row

    row = asyncio.run(scenario())
    for key, value in VALUES.items():
        assert row.get(key) == value


def test_schedule_flush_defers_while_a_flush_is_in_flight():
    from app.collab.materializer import Materializer

    mat = Materializer(project_id=1, flask_app=None)
    mat._flushing = True
    mat.schedule_flush()
    assert mat._dirty_again is True


def test_note_external_write_invalidates_a_snapshot_epoch():
    from app.collab.materializer import Materializer

    mat = Materializer(project_id=1, flask_app=None)
    epoch = mat._wb_epoch
    mat.note_external_write()
    assert mat._wb_epoch != epoch
