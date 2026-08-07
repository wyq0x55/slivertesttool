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
    # Real Y updates, so merge_updates() has something valid to squash.
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
