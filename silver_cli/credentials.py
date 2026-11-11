"""Saved logins: one personal API token per server, written by ``login``.

``login`` exchanges a username/password for a token once and stores it here,
so later commands need neither the password nor ``SILVER_CLI_TOKEN``. The
file holds only tokens (never passwords) and is created owner-only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path:
    override = os.environ.get("SILVER_CLI_CONFIG")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    root = Path(base) if base else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "silver-cli" / "credentials.json"


def _key(url: str) -> str:
    return url.rstrip("/")


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"servers": {}}
    if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
        return {"servers": {}}
    return data


def _write(data: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(temporary, target)


def saved(url: str | None) -> tuple[str | None, dict | None]:
    """The server URL to use (explicit, else the last login) and its saved entry."""
    data = load()
    url = _key(url) if url else data.get("default_url")
    entry = data["servers"].get(url) if url else None
    return url, entry if isinstance(entry, dict) and entry.get("token") else None


def save(url: str, entry: dict) -> Path:
    data = load()
    data["servers"][_key(url)] = entry
    data["default_url"] = _key(url)
    _write(data)
    return path()


def forget(url: str) -> bool:
    data = load()
    removed = data["servers"].pop(_key(url), None) is not None
    if data.get("default_url") == _key(url):
        data.pop("default_url")
    _write(data)
    return removed
