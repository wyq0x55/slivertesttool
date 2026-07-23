"""Admin-registered server-side ``.sil`` plant models.

The Silver plant model is a shared asset that lives **on the server's file
system**. Rather than uploading a ``.sil`` file, an administrator registers one
or more absolute ``.sil`` *paths* (see the admin page). Testers then pick one of
those registered models by name when submitting a test; the model file itself is
never uploaded and is opened in place, so its own relative resource references
keep resolving.

The registered models are stored as a JSON array in the ``app_settings`` table
under :data:`Setting.SIL_MODELS` so both the web and worker processes see the
same list::

    [{"name": "PlantA", "path": "/opt/models/plant_a.sil"}, ...]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..extensions import db
from ..models import Setting


class ModelError(Exception):
    """Raised for invalid model registrations."""


# --------------------------------------------------------------------------- #
# Low-level persistence
# --------------------------------------------------------------------------- #
def _load_raw() -> List[dict]:
    row = db.session.get(Setting, Setting.SIL_MODELS)
    if row is None or not row.value:
        return []
    try:
        data = json.loads(row.value)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: List[dict] = []
    for item in data:
        if isinstance(item, dict) and item.get("name") and item.get("path"):
            out.append({"name": str(item["name"]), "path": str(item["path"])})
    return out


def _save_raw(models: List[dict]) -> None:
    row = db.session.get(Setting, Setting.SIL_MODELS)
    value = json.dumps(models, ensure_ascii=False)
    if row is None:
        db.session.add(Setting(key=Setting.SIL_MODELS, value=value))
    else:
        row.value = value
    db.session.commit()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def list_models(include_path: bool = False) -> List[dict]:
    """Return the registered models.

    Each entry is ``{"name", "exists"}`` (and ``"path"`` when *include_path*).
    ``exists`` reflects whether the ``.sil`` file is currently present on disk.
    """
    result = []
    for m in _load_raw():
        entry = {"name": m["name"], "exists": Path(m["path"]).is_file()}
        if include_path:
            entry["path"] = m["path"]
        result.append(entry)
    return result


def has_models() -> bool:
    return bool(_load_raw())


def get_model_path(name: str) -> Optional[Path]:
    """Resolve a registered model *name* to its server-side path, or ``None``."""
    if not name:
        return None
    for m in _load_raw():
        if m["name"] == name:
            return Path(m["path"])
    return None


def default_model() -> Optional[dict]:
    """Return the first registered model (name/path), or ``None``."""
    models = _load_raw()
    return models[0] if models else None


def add_model(name: str, path: str) -> dict:
    """Register a server-side ``.sil`` path under a unique display *name*.

    The path must end in ``.sil``. A still-missing path is accepted (with
    ``exists=False``) so a model can be pre-registered. Raises
    :class:`ModelError` on bad input.
    """
    name = (name or "").strip()
    path = (path or "").strip().strip('"')
    if not path:
        raise ModelError("A .sil path is required.")
    if not path.lower().endswith(".sil"):
        raise ModelError("The path must point to a Silver model file (*.sil).")
    if not name:
        name = Path(path).stem or Path(path).name
    models = _load_raw()
    for m in models:
        if m["name"] == name:
            raise ModelError(f"A model named '{name}' is already registered.")
        if str(Path(m["path"])) == str(Path(path)):
            raise ModelError(f"That path is already registered as '{m['name']}'.")
    entry = {"name": name, "path": path}
    models.append(entry)
    _save_raw(models)
    return {"name": name, "path": path, "exists": Path(path).is_file()}


def remove_model(name: str) -> bool:
    """Remove a registered model by *name*. Returns True if one was removed."""
    models = _load_raw()
    kept = [m for m in models if m["name"] != name]
    if len(kept) == len(models):
        return False
    _save_raw(kept)
    return True


def replace_models(entries: List[dict]) -> List[dict]:
    """Overwrite the whole model list (used by the bulk paths editor).

    ``entries`` is a list of ``{"name"?, "path"}`` dicts; empty paths are
    skipped and names default to the file stem. Raises :class:`ModelError` on a
    non-``.sil`` path or a duplicate name.
    """
    cleaned: List[dict] = []
    seen_names: set = set()
    for item in entries:
        name = (item.get("name") or "").strip()
        path = (item.get("path") or "").strip().strip('"')
        if not path:
            continue
        if not path.lower().endswith(".sil"):
            raise ModelError(f"Not a .sil path: {path}")
        if not name:
            name = Path(path).stem or Path(path).name
        if name in seen_names:
            raise ModelError(f"Duplicate model name: {name}")
        seen_names.add(name)
        cleaned.append({"name": name, "path": path})
    _save_raw(cleaned)
    return list_models(include_path=True)
