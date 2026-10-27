"""Job-local, hash-bound diagnostics of the actual input-model exchange."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def save(root: Path, name: str, data: bytes, meta: dict[str, Any]) -> None:
    """Write exclusively; the caller provides a new directory for each call."""
    path = root / name
    with path.open("xb") as output:
        output.write(data)
    meta["artifacts"].append({"id": hashlib.sha256(name.encode("utf-8")).hexdigest()[:16],
                              "name": name, "sha256": hashlib.sha256(data).hexdigest()})
