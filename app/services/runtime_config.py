"""Runtime-adjustable, cross-process configuration overrides.

A curated subset of the ``.env`` knobs can be changed *live* from the system
admin console (「授权 / 并发」) without restarting the web process or the Huey
worker. Just like the license gate (:mod:`app.services.license_service`) the
overrides live in the shared ``app_settings`` PostgreSQL table so both processes
observe the same value, and each override is re-read at its live application
point (the worker reconcile loop / per-task runner) rather than cached at start.

Only options that have a genuine live application point are exposed here. Values
that can only take effect at process start (e.g. ``HUEY_WORKERS`` -- the Huey
consumer thread-pool size -- or ``RUNNER_BACKEND`` -- fixed once the Silver
instance pool driver is built) are intentionally *not* listed; changing those
still requires an ``.env`` edit and a restart.

Design
------
* Each field carries typed metadata (``FieldSpec``) plus the ``Config`` attribute
  that provides its *default* (the value read from ``.env`` at start-up). When no
  override row exists the default is returned, so the platform behaves exactly as
  before until an admin sets a value.
* Overrides are stored under a ``cfg:<key>`` namespace in ``app_settings`` to
  avoid clashing with the license gate rows (``license_limit`` / ``license_inuse``).
* ``license_limit`` is deliberately handled by :mod:`app.services.license_service`
  (it also drives the live in-use counter and pool target) and is *not* stored
  here; the admin API surfaces it through the existing license endpoint.

All reads require an active Flask application context (they touch the database).
Every getter degrades gracefully to the ``Config`` default if the row is missing
or holds an unparseable value, so a malformed override can never break a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..config import Config
from ..extensions import db
from ..models import Setting

# Namespace prefix for override rows in ``app_settings``.
_PREFIX = "cfg:"

# Truthy / falsy tokens accepted when coercing a stored string to bool.
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class FieldSpec:
    """Metadata describing one runtime-adjustable configuration field."""

    key: str                    # storage key (also the API/JS field id)
    type: str                   # "int" | "float" | "bool"
    config_attr: str            # Config attribute holding the .env default
    min: Optional[float] = None
    max: Optional[float] = None
    group: str = "concurrency"  # UI grouping hint


# The exposed, hot-reloadable fields. ``license_limit`` is handled separately by
# ``license_service`` and rendered by the existing 并发上限 control, so it is not
# repeated here.
FIELDS: List[FieldSpec] = [
    FieldSpec("silver_pool_enabled", "bool", "SILVER_POOL_ENABLED", group="pool"),
    FieldSpec("silver_pool_prewarm", "bool", "SILVER_POOL_PREWARM", group="pool"),
    FieldSpec(
        "silver_pool_reconcile_seconds", "float", "SILVER_POOL_RECONCILE_SECONDS",
        min=0.5, max=3600, group="pool",
    ),
    FieldSpec(
        "execution_timeout", "int", "EXECUTION_TIMEOUT",
        min=1, max=86400, group="runner",
    ),
    FieldSpec("silver_gui", "bool", "SILVER_GUI", group="runner"),
    FieldSpec(
        "task_event_retention", "int", "TASK_EVENT_RETENTION",
        min=100, max=1_000_000, group="maintenance",
    ),
]

_BY_KEY: Dict[str, FieldSpec] = {f.key: f for f in FIELDS}


# --------------------------------------------------------------------------- #
# Coercion helpers
# --------------------------------------------------------------------------- #
def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    try:
        return int(token) != 0
    except (TypeError, ValueError):
        return default


def _coerce_number(spec: FieldSpec, value: Any, default: float) -> float:
    try:
        num = int(value) if spec.type == "int" else float(value)
    except (TypeError, ValueError):
        return default
    if spec.min is not None and num < spec.min:
        num = spec.min if spec.type != "int" else int(spec.min)
    if spec.max is not None and num > spec.max:
        num = spec.max if spec.type != "int" else int(spec.max)
    return num


_COERCERS: Dict[str, Callable[[FieldSpec, Any, Any], Any]] = {
    "bool": lambda spec, v, d: _coerce_bool(v, d),
    "int": lambda spec, v, d: int(_coerce_number(spec, v, d)),
    "float": lambda spec, v, d: float(_coerce_number(spec, v, d)),
}


def _default(spec: FieldSpec) -> Any:
    """The .env-derived default for a field (read from :class:`Config`)."""
    return getattr(Config, spec.config_attr)


def _stored_value(key: str) -> Optional[str]:
    row = db.session.get(Setting, _PREFIX + key)
    return None if row is None else row.value


# --------------------------------------------------------------------------- #
# Public read API (require an app context)
# --------------------------------------------------------------------------- #
def get(key: str) -> Any:
    """Return the effective, typed value for *key* (override or default)."""
    spec = _BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"Unknown runtime config key: {key}")
    default = _default(spec)
    raw = _stored_value(key)
    if raw is None:
        return default
    return _COERCERS[spec.type](spec, raw, default)


def get_bool(key: str) -> bool:
    return bool(get(key))


def get_int(key: str) -> int:
    return int(get(key))


def get_float(key: str) -> float:
    return float(get(key))


def values() -> Dict[str, Any]:
    """Effective values for every exposed field, keyed by field id."""
    return {spec.key: get(spec.key) for spec in FIELDS}


def describe() -> List[Dict[str, Any]]:
    """Field metadata + current values for the admin API / UI to render."""
    out: List[Dict[str, Any]] = []
    for spec in FIELDS:
        out.append(
            {
                "key": spec.key,
                "type": spec.type,
                "group": spec.group,
                "min": spec.min,
                "max": spec.max,
                "default": _default(spec),
                "value": get(spec.key),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Public write API
# --------------------------------------------------------------------------- #
def _validate(spec: FieldSpec, value: Any) -> Any:
    """Validate + normalise one incoming value; raise ``ValueError`` on failure."""
    if spec.type == "bool":
        return _coerce_bool(value, _default(spec))
    try:
        num = int(value) if spec.type == "int" else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{spec.key} 需要一个{'整数' if spec.type == 'int' else '数值'}")
    if spec.min is not None and num < spec.min:
        raise ValueError(f"{spec.key} 不能小于 {spec.min}")
    if spec.max is not None and num > spec.max:
        raise ValueError(f"{spec.key} 不能大于 {spec.max}")
    return num


def _store(key: str, value: Any) -> None:
    row = db.session.get(Setting, _PREFIX + key)
    if isinstance(value, bool):
        text = "1" if value else "0"
    else:
        text = str(value)
    if row is None:
        db.session.add(Setting(key=_PREFIX + key, value=text))
    else:
        row.value = text


def set_many(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and persist a batch of overrides. Returns the new values map.

    Unknown keys are ignored (forward compatible with older clients); an invalid
    value raises ``ValueError`` and nothing is committed.
    """
    normalised: Dict[str, Any] = {}
    for key, raw in (changes or {}).items():
        spec = _BY_KEY.get(key)
        if spec is None:
            continue
        normalised[key] = _validate(spec, raw)
    for key, value in normalised.items():
        _store(key, value)
    if normalised:
        db.session.commit()
    return values()
