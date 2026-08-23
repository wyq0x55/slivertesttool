"""AI provider configuration (Setting table first, environment fallback).

The platform itself runs on an internal network, but may reach an external or
intranet-hosted OpenAI-compatible endpoint. Configuration precedence:

    1. ``app_settings`` rows (admin-editable at runtime, shared by web+worker)
    2. environment variables (bootstrap default for fresh installs)

Setting keys / env names:

    ai_api_base   / SILVERTOOL_AI_API_BASE    e.g. https://api.example.com/v1
    ai_api_key    / SILVERTOOL_AI_API_KEY
    ai_model      / SILVERTOOL_AI_MODEL       e.g. glm-4.7 / deepseek-chat
    ai_timeout    / SILVERTOOL_AI_TIMEOUT     seconds, default 120
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ...extensions import db
from ...models.setting import Setting

KEY_API_BASE = "ai_api_base"
KEY_API_KEY = "ai_api_key"
KEY_MODEL = "ai_model"
KEY_TIMEOUT = "ai_timeout"

_ENV = {
    KEY_API_BASE: "SILVERTOOL_AI_API_BASE",
    KEY_API_KEY: "SILVERTOOL_AI_API_KEY",
    KEY_MODEL: "SILVERTOOL_AI_MODEL",
    KEY_TIMEOUT: "SILVERTOOL_AI_TIMEOUT",
}

DEFAULT_TIMEOUT = 120

# Never leak the key through any API response.
_SECRET_KEYS = {KEY_API_KEY}


def _from_settings(key: str) -> Optional[str]:
    try:
        row = db.session.get(Setting, key)
    except Exception:  # noqa: BLE001 - settings table may not exist yet
        return None
    if row is None or row.value is None or row.value.strip() == "":
        return None
    return row.value.strip()


def get_ai_config(*, include_secret: bool = False) -> dict[str, Any]:
    """Resolve the effective AI configuration.

    ``include_secret=False`` (the default) masks the API key as ``***set***``
    or ``***unset***`` — the settings endpoint must never echo the key back.
    """
    resolved: dict[str, Any] = {}
    for key in (KEY_API_BASE, KEY_API_KEY, KEY_MODEL, KEY_TIMEOUT):
        value = _from_settings(key) or os.environ.get(_ENV[key], "")
        if key == KEY_TIMEOUT:
            try:
                resolved[key] = int(value) if value else DEFAULT_TIMEOUT
            except ValueError:
                resolved[key] = DEFAULT_TIMEOUT
        else:
            resolved[key] = value
    if not include_secret:
        resolved[KEY_API_KEY] = "***set***" if resolved[KEY_API_KEY] else "***unset***"
    return resolved


def is_configured() -> bool:
    cfg = get_ai_config(include_secret=True)
    return bool(cfg[KEY_API_BASE]) and bool(cfg[KEY_API_KEY]) and bool(cfg[KEY_MODEL])


def update_ai_config(values: dict[str, Any]) -> dict[str, Any]:
    """Persist non-empty provided values into ``app_settings``.

    A masked ``***set***`` / empty key value is ignored, so the settings UI can
    round-trip the masked form without wiping the stored secret.
    """
    for key in (KEY_API_BASE, KEY_API_KEY, KEY_MODEL, KEY_TIMEOUT):
        if key not in values:
            continue
        value = str(values.get(key) or "").strip()
        if key in _SECRET_KEYS and value in ("", "***set***", "***unset***"):
            continue
        if not value:
            continue
        row = db.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value
    db.session.commit()
    return get_ai_config()
