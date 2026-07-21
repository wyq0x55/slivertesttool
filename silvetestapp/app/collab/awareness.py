"""Awareness → row-actor attribution (pure, ``pycrdt``-free).

The collaboration clients broadcast a small Awareness state over the shared
WebSocket (design §6.1)::

    provider.awareness.setLocalState({
      user:      { id, name, color },
      cursor:    { sheet: 'test', uuid, col },
      selection: { sheet, uuid, c1, c2 },
    })

Awareness is ephemeral (never enters the CRDT, never persisted). We use it as a
**best-effort** signal for *who is editing which row* so the debounced
materializer can attribute each row's ``updated_by`` to the collaborator whose
cursor/selection sits on that row, instead of a single fixed batch actor
(design §7.1 step 4, checklist Phase 1.2). When Awareness is unavailable or a
row has no cursor on it, materialization falls back to the batch actor — so this
module never changes correctness, only audit attribution.

Kept free of any ``pycrdt`` import so the logic is unit-testable in the sandbox
(no CRDT runtime). :func:`snapshot_states` is the only place that touches a live
Awareness object and is written defensively against its exact API shape.
"""

from __future__ import annotations

from typing import Any, Optional


def _coerce_uid(raw: Any) -> Optional[int]:
    """Best-effort ``user.id`` -> ``int`` (Awareness values are JSON-ish)."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _row_uuid(node: Any) -> Optional[str]:
    """Extract a non-empty ``uuid`` from a cursor/selection sub-state."""
    if not isinstance(node, dict):
        return None
    val = node.get("uuid")
    if not isinstance(val, str):
        return None
    val = val.strip()
    return val or None


def row_actors(states: dict[Any, dict]) -> dict[str, int]:
    """Map row ``uuid -> user_id`` from a snapshot of Awareness states.

    ``states`` is ``{client_id: state_dict}`` exactly as an Awareness object
    yields it. For every client that has a ``user.id`` and a ``cursor`` (or,
    lacking a cursor, a ``selection``) pointing at a row ``uuid``, that user is
    recorded as the row's presumed editor.

    Heuristic + deterministic: a row may have several collaborators' cursors on
    it at once and Awareness carries no per-field timestamp, so we resolve ties
    deterministically by iterating clients in ascending ``client_id`` order and
    letting the highest ``client_id`` win (stable across runs given the same
    snapshot). Rows nobody is focused on are simply absent from the result and
    keep the batch actor downstream.
    """
    out: dict[str, int] = {}
    if not isinstance(states, dict):
        return out

    def _sort_key(item):
        cid = item[0]
        try:
            return (0, int(cid))
        except (TypeError, ValueError):
            return (1, str(cid))

    for _client_id, state in sorted(states.items(), key=_sort_key):
        if not isinstance(state, dict):
            continue
        uid = _coerce_uid((state.get("user") or {}).get("id")
                          if isinstance(state.get("user"), dict) else None)
        if uid is None:
            continue
        row_uuid = _row_uuid(state.get("cursor")) or _row_uuid(state.get("selection"))
        if row_uuid is None:
            continue
        out[row_uuid] = uid
    return out


def snapshot_states(awareness: Any) -> dict[Any, dict]:
    """Return a plain ``{client_id: state}`` dict from a live Awareness object.

    Defensive against the exact ``pycrdt`` Awareness API: tries the ``states``
    property first, then a ``get_states()`` method, and always returns a plain
    ``dict`` (copied) so the caller can hand it to a worker thread safely. Any
    failure yields ``{}`` — attribution then falls back to the batch actor.
    """
    if awareness is None:
        return {}
    raw: Any = None
    try:
        raw = getattr(awareness, "states", None)
        if raw is None:
            getter = getattr(awareness, "get_states", None)
            if callable(getter):
                raw = getter()
    except Exception:  # pragma: no cover - never break materialization
        return {}
    if not isinstance(raw, dict):
        return {}
    # Shallow-copy so a concurrent Awareness mutation cannot corrupt our scan.
    return dict(raw)
