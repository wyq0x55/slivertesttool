"""DB-free unit tests for the collab presence single-writer boundary logic.

Only the pure freshness predicate is exercised here (mirrors
``test_collab_awareness.py``): it needs no Flask app, no DB and no pycrdt, so it
runs in any environment. The DB-backed upsert/read helpers are covered by the
integration smoke test once pycrdt/PostgreSQL are available.
"""

import datetime as _dt

from app.collab import presence


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def test_active_when_fresh_and_connected():
    assert presence.is_fresh(2, _now(), ttl_seconds=30) is True


def test_inactive_when_no_connections():
    assert presence.is_fresh(0, _now(), ttl_seconds=30) is False
    assert presence.is_fresh(None, _now(), ttl_seconds=30) is False


def test_inactive_when_stale():
    old = _now() - _dt.timedelta(seconds=120)
    assert presence.is_fresh(1, old, ttl_seconds=30) is False


def test_boundary_exactly_at_ttl_is_active():
    edge = _now() - _dt.timedelta(seconds=30)
    # age == ttl is still considered fresh (<=).
    assert presence.is_fresh(1, edge, ttl_seconds=30, now=edge + _dt.timedelta(seconds=30)) is True


def test_naive_timestamp_is_treated_as_utc():
    naive = _dt.datetime.utcnow()  # no tzinfo, as some DB drivers return
    assert presence.is_fresh(1, naive, ttl_seconds=60) is True


def test_zero_ttl_disables_freshness_gate():
    ancient = _now() - _dt.timedelta(days=3650)
    assert presence.is_fresh(1, ancient, ttl_seconds=0) is True
