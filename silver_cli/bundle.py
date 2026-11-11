"""Package this CLI as a single-file Python zipapp.

The archive holds only the standard-library CLI modules and the generated
operation catalogue, so users run it on their own machine against a deployed
server with ``python silver-cli.pyz ...`` and need no platform checkout.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent
_MAIN = b"from silver_cli.runner import main\n\nraise SystemExit(main())\n"
# Fixed timestamps keep the archive byte-identical for identical sources.
_STAMP = (2020, 1, 1, 0, 0, 0)


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_STAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def build() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_entry("__main__.py"), _MAIN)
        for path in sorted(_PACKAGE.iterdir()):
            if path.is_file() and path.suffix in {".py", ".json"}:
                archive.writestr(_entry(f"silver_cli/{path.name}"), path.read_bytes())
    return b"#!/usr/bin/env python3\n" + buffer.getvalue()
