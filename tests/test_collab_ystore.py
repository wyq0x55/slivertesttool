"""PgYStore append log: sequence integrity and compaction.

The store caches the next ``seq`` in memory so the hot write path is a single
INSERT instead of ``SELECT max(seq)`` + INSERT. That cache is exactly the kind
of optimisation that corrupts an append-only log quietly, so the ordering and
resume behaviour is pinned here.

The synchronous helpers are exercised directly: ``write()``/``read()`` only add
an anyio lock and a worker-thread dispatch on top of them, while every seq
decision lives in ``_write_sync``/``_compact_locked``.
"""

from __future__ import annotations

import pytest

pycrdt = pytest.importorskip("pycrdt")


@pytest.fixture()
def store(app_ctx):
    """A PgYStore bound to a real project row."""
    from app.collab.pg_ystore import PgYStore
    from app.extensions import db
    from app.models import LMUser, Project

    with app_ctx.app_context():
        user = LMUser(username="ystore_user", display_name="Y")
        db.session.add(user)
        db.session.flush()
        project = Project(code="YSTORE", name="Store", status="active",
                          owner_id=user.id, created_by=user.id)
        db.session.add(project)
        db.session.commit()
        yield PgYStore(f"project:{project.id}", app_ctx), app_ctx, project.id


def _seqs(app, pid):
    from app.models import CollabDoc
    with app.app_context():
        return [r.seq for r in CollabDoc.query.filter_by(project_id=pid)
                .order_by(CollabDoc.seq.asc()).all()]


def test_writes_get_consecutive_seqs(store):
    st, app, pid = store
    for i in range(5):
        st._write_sync(b"u%d" % i, None, 1.0 + i)
    assert _seqs(app, pid) == [1, 2, 3, 4, 5]


def test_read_returns_updates_in_write_order(store):
    st, app, pid = store
    payloads = [b"first", b"second", b"third"]
    for i, p in enumerate(payloads):
        st._write_sync(p, None, 1.0 + i)
    with app.app_context():
        assert [u for u, _md, _ts in st._read_sync()] == payloads


def test_fresh_store_resumes_after_existing_rows(store):
    """A store created against a project that already has a log must not
    restart at seq 1 and collide with what is already there."""
    from app.collab.pg_ystore import PgYStore
    st, app, pid = store
    for i in range(3):
        st._write_sync(b"old%d" % i, None, 1.0)

    fresh = PgYStore(f"project:{pid}", app)
    assert fresh._next_seq is None            # nothing cached yet
    fresh._write_sync(b"new", None, 2.0)
    assert _seqs(app, pid) == [1, 2, 3, 4]


def test_cached_seq_is_dropped_when_a_write_fails(store):
    """A failed commit must not leave the cache pointing past reality."""
    st, app, pid = store
    st._write_sync(b"ok", None, 1.0)
    assert st._next_seq == 2

    from app.extensions import db
    real_commit = db.session.commit

    def boom():
        raise RuntimeError("commit exploded")

    with app.app_context():
        db.session.commit = boom
        try:
            with pytest.raises(RuntimeError):
                st._write_sync(b"doomed", None, 2.0)
        finally:
            db.session.commit = real_commit

    assert st._next_seq is None, "a failed write must force a re-read"
    st._write_sync(b"after", None, 3.0)
    assert _seqs(app, pid) == [1, 2]


def test_compaction_squashes_the_log_and_keeps_writing(store):
    """Past the threshold the log collapses to one row, and the next write
    continues from there instead of colliding with seq 1."""
    from app.collab import pg_ystore
    from pycrdt import Doc, Map

    st, app, pid = store
    # Real Y updates, so compaction has valid CRDT updates to squash.
    doc = Doc()
    doc["m"] = m = Map()
    updates = []
    for i in range(4):
        before = doc.get_state()
        with doc.transaction():
            m[f"k{i}"] = i
        updates.append(doc.get_update(before))

    # Threshold 4 => the 4th write is the one that triggers compaction.
    original = pg_ystore.COMPACT_THRESHOLD
    pg_ystore.COMPACT_THRESHOLD = 4
    try:
        for i, u in enumerate(updates):
            st._write_sync(u, None, 1.0 + i)
    finally:
        pg_ystore.COMPACT_THRESHOLD = original

    assert _seqs(app, pid) == [1], "log should be squashed into a single row"
    assert st._next_seq == 2

    st._write_sync(b"after-compaction", None, 9.0)
    assert _seqs(app, pid) == [1, 2]


def test_compacted_log_still_rebuilds_the_document(store):
    """Compaction must preserve state, not just row count."""
    from app.collab import pg_ystore
    from pycrdt import Doc, Map

    st, app, pid = store
    doc = Doc()
    doc["m"] = m = Map()
    for i in range(5):
        # Diff against the pre-transaction state vector so each row carries ONLY
        # its own key. doc.get_update() with no argument returns the full state
        # every time, which would make dropping any single row undetectable.
        before = doc.get_state()
        with doc.transaction():
            m[f"k{i}"] = i
        st._write_sync(doc.get_update(before), None, 1.0 + i)

    with app.app_context():
        st._compact_locked()
        rows = st._read_sync()
    assert len(rows) == 1

    rebuilt = Doc()
    rebuilt["m"] = rm = Map()
    rebuilt.apply_update(rows[0][0])
    assert dict(rm) == {f"k{i}": i for i in range(5)}


@pytest.fixture()
def isolated_collab_app(tmp_path):
    from app import create_app
    from app.config import Config
    from app.extensions import db
    from app.models import FieldDefinition, LMUser, Project

    class IsolatedConfig(Config):
        TESTING = True
        SECRET_KEY = "ystore-isolated-test-secret-key-32b"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{(tmp_path / 'ystore.db').as_posix()}"
        SQLALCHEMY_ENGINE_OPTIONS = {}

    app = create_app(IsolatedConfig)
    with app.app_context():
        db.create_all()
        user = LMUser(username="collab_owner", display_name="Owner")
        db.session.add(user)
        db.session.flush()
        project = Project(
            code="COLLAB",
            name="Collab Store",
            status="active",
            owner_id=user.id,
            created_by=user.id,
        )
        db.session.add(project)
        db.session.flush()
        for sheet, field_key, data_type in (
            ("test", "description", "text"),
            ("test", "steps", "steps"),
            ("lib", "lib_stb", "steps"),
        ):
            db.session.add(
                FieldDefinition(
                    project_id=project.id,
                    field_key=field_key,
                    display_name=field_key,
                    data_type=data_type,
                    sheet=sheet,
                    is_active=True,
                )
            )
        db.session.commit()
        yield app, user.id, project.id
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_compaction_handles_multi_session_overlapping_updates_and_tombstones(
    isolated_collab_app,
):
    from app.collab import doc_model
    from app.collab.pg_ystore import PgYStore
    from pycrdt import Array, Doc, Map

    app, _uid, pid = isolated_collab_app
    ystore = PgYStore(f"project:{pid}", app)
    written: list[bytes] = []

    seed = Doc()
    doc_model.ensure_sheets(seed)
    with seed.transaction():
        test_rows = seed.get("rows:test", type=Array)
        for idx in range(8):
            test_rows.append(
                Map({"uuid": f"row-{idx}", "case_id": f"TC-{idx}", "version": 1})
            )
    first = seed.get_update()
    written.append(first)
    ystore._write_sync(first, None, 1.0)

    timestamp = 2.0
    for session in range(1, 18):
        hydrated = Doc()
        for raw in written:
            hydrated.apply_update(raw)
        doc_model.ensure_sheets(hydrated)
        test_rows = hydrated.get("rows:test", type=Array)
        receipts = hydrated.get("server_writeback_receipts", type=Map)

        for step in range(3):
            before_tx1 = hydrated.get_state()
            with hydrated.transaction():
                if step == 0 and len(test_rows) > 3:
                    del test_rows[0]
                    test_rows.insert(
                        0,
                        Map(
                            {
                                "uuid": f"row-s{session}",
                                "case_id": f"TC-S{session}",
                                "version": 1,
                            }
                        ),
                    )
                target = test_rows[step % len(test_rows)]
                target["title"] = f"edit-{session}-{step}"
                receipts[str(pid)] = str(session * 10 + step)
            inc1 = hydrated.get_update(before_tx1)

            before_tx2 = hydrated.get_state()
            with hydrated.transaction():
                target["version"] = session * 10 + step
            inc2 = hydrated.get_update(before_tx2)
            combined = hydrated.get_update(before_tx1)
            full_snapshot = hydrated.get_update()

            for payload in (inc1, combined, inc2, full_snapshot):
                written.append(payload)
                ystore._write_sync(payload, None, timestamp)
                timestamp += 1.0

    assert len(written) > 200
    assert _seqs(app, pid) < [len(written)], "log must have compacted past threshold"

    expected = Doc()
    for raw in written:
        expected.apply_update(raw)
    doc_model.ensure_sheets(expected)

    rebuilt = Doc()
    with app.app_context():
        for update_bytes, _meta, _ts in ystore._read_sync():
            rebuilt.apply_update(update_bytes)
    doc_model.ensure_sheets(rebuilt)

    assert doc_model.content_hash(rebuilt) == doc_model.content_hash(expected)
    assert dict(rebuilt.get("server_writeback_receipts", type=Map)) == dict(
        expected.get("server_writeback_receipts", type=Map)
    )


def test_nested_steps_hash_is_stable_and_first_reopen_compacts_cleanly(
    isolated_collab_app, caplog
):
    import asyncio
    import logging
    from app.collab import doc_model, pg_ystore
    from app.collab.pg_ystore import PgYStore
    from app.collab.server import CollabWebsocketServer
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import items_service
    from pycrdt import Array, Doc, Map

    app, uid, pid = isolated_collab_app
    with app.app_context():
        user = db.session.get(LMUser, uid)
        project = db.session.get(Project, pid)
        test_item = items_service.create_item(
            user,
            project,
            {"case_id": "TC-NESTED-1", "title": "Initial", "sheet": "test"},
        )
        lib_item = items_service.create_item(
            user,
            project,
            {"case_id": "LIB-NESTED-1", "title": "Lib", "sheet": "lib"},
        )
        test_id, test_uuid = test_item.id, test_item.uuid
        lib_uuid = lib_item.uuid

    async def setup_nested_crdt_and_fill_log():
        async with CollabWebsocketServer(app) as server:
            room = await server.get_room(f"project:{pid}")
            with room.ydoc.transaction():
                t_row = room.ydoc.get("rows:test", type=Array)[0]
                t_row["steps"] = Map(
                    {
                        "steps": Array(
                            [
                                Map(
                                    {
                                        "no": 1.0,
                                        "purpose": "init",
                                        "operation": "op",
                                        "timing": "-",
                                        "inputs": Array(["1"]),
                                        "expecteds": Array(["1"]),
                                        "subroutine": None,
                                        "args": None,
                                    }
                                )
                            ]
                        ),
                        "input_signals": Array([Array(["sig", "path"])]),
                        "expected_signals": Array([]),
                    }
                )
                l_row = room.ydoc.get("rows:lib", type=Array)[0]
                l_row["lib_stb"] = Map(
                    {
                        "steps": Array([Map({"no": 1.0, "operation": "lib-op", "timing": "-"})]),
                        "input_signals": Array([]),
                        "expected_signals": Array([]),
                    }
                )
            await server._materializers[f"project:{pid}"]._flush()

            assert doc_model.write_row_fields(
                room.ydoc,
                "test",
                {
                    test_uuid: {
                        "steps": {
                            "expected_signals": [],
                            "input_signals": [["sig", "path"]],
                            "steps": [
                                {
                                    "args": None,
                                    "expecteds": ["1"],
                                    "inputs": ["1"],
                                    "no": 1.0,
                                    "operation": "op",
                                    "purpose": "init",
                                    "subroutine": None,
                                    "timing": "-",
                                }
                            ],
                        }
                    }
                },
            ) == 0
            assert isinstance(room.ydoc.get("rows:test", type=Array)[0]["steps"], Map)

    asyncio.run(setup_nested_crdt_and_fill_log())

    ystore = PgYStore(f"project:{pid}", app)
    with app.app_context():
        stored_updates = [raw for raw, _meta, _ts in ystore._read_sync()]

    hashes = set()
    for _ in range(10):
        fresh = Doc()
        for raw in stored_updates:
            fresh.apply_update(raw)
        doc_model.ensure_sheets(fresh)
        hashes.add(doc_model.content_hash(fresh))
    assert len(hashes) == 1, "content_hash must not depend on per-Doc HashMap key order"

    with app.app_context():
        row = db.session.get(TestItemRow, test_id)
        version_before_reopen = row.version
        items_service.update_item(
            db.session.get(LMUser, uid),
            db.session.get(Project, pid),
            row,
            row.version,
            {"description": "Offline Edit"},
        )
        version_after_offline = db.session.get(TestItemRow, test_id).version

    original_threshold = pg_ystore.COMPACT_THRESHOLD
    pg_ystore.COMPACT_THRESHOLD = 2
    caplog.set_level(logging.WARNING, logger="collab.server")
    try:
        async def reopen_and_flush():
            async with CollabWebsocketServer(app) as server:
                room = await server.get_room(f"project:{pid}")
                rows = {r["uuid"]: r for r in doc_model.snapshot_sheet(room.ydoc, "test")}
                assert rows[test_uuid]["description"] == "Offline Edit"
                await server._materializers[f"project:{pid}"]._flush()

        asyncio.run(reopen_and_flush())
    finally:
        pg_ystore.COMPACT_THRESHOLD = original_threshold

    assert "never materialized" not in caplog.text
    assert _seqs(app, pid) == [1]
    with app.app_context():
        assert version_after_offline == version_before_reopen + 1
        assert db.session.get(TestItemRow, test_id).version == version_after_offline
        lib_row = TestItemRow.query.filter_by(project_id=pid, uuid=lib_uuid).one()
        assert lib_row.version == 2
