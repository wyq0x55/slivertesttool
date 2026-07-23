"""Installer for the real-time collaboration dependencies (run_collab.py).

Run with the SAME interpreter you use for ``run_collab.py``. The target runtime
is **CPython 3.10** (the app's supported version), but this script adapts to
whatever interpreter launches it.

    python scripts/install_collab_deps.py

``pycrdt`` is a compiled (Rust) extension, so it needs a wheel matching your
exact Python version + OS. The vendored ``vendor/collab-wheels`` set ships the
pure-python deps (any Python) plus a couple of pycrdt binaries (cp311-win /
cp312-linux). If none of the vendored pycrdt binaries matches your interpreter
(e.g. you run Python 3.10), this script falls back to a HYBRID install: the
vendored wheels are still preferred via ``--find-links`` while pip fetches the
correct ``pycrdt`` binary from PyPI (which publishes cp310 wheels for Win/Linux/
macOS).

Fully offline on 3.10? Drop a matching ``pycrdt-0.14.1-cp310-...whl`` into
``vendor/collab-wheels`` first (download it once with ``pip download
pycrdt==0.14.1`` on a machine with the same OS/Python), then re-run this script.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_WHEELS = os.path.join(_ROOT, "vendor", "collab-wheels")
_REQ = os.path.join(_ROOT, "requirements-collab.txt")


def _abi_tag() -> str:
    """e.g. 'cp310' for the running interpreter."""
    return "cp{}{}".format(sys.version_info[0], sys.version_info[1])


def _platform_fragment() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _has_matching_pycrdt_wheel() -> bool:
    """True if a vendored pycrdt binary matches this interpreter's ABI + OS."""
    abi = _abi_tag()
    frag = _platform_fragment()
    for whl in glob.glob(os.path.join(_WHEELS, "pycrdt-*.whl")):
        name = os.path.basename(whl).lower()
        if abi not in name:
            continue
        if frag == "win" and "win" in name:
            return True
        if frag == "macos" and "macos" in name:
            return True
        if frag == "linux" and ("linux" in name or "manylinux" in name):
            return True
    return False


def _run(cmd: "list[str]") -> int:
    print("+", " ".join(cmd))
    return subprocess.call(cmd)


def _has_vendored_ws() -> bool:
    """True if a WebSocket protocol wheel (websockets/wsproto) is vendored."""
    for whl in glob.glob(os.path.join(_WHEELS, "*.whl")):
        name = os.path.basename(whl).lower()
        if name.startswith("websockets") or name.startswith("wsproto"):
            return True
    return False


def _verify_ws() -> None:
    """Warn loudly if uvicorn has no WebSocket protocol library available.

    Without one, uvicorn downgrades the collab WebSocket upgrade to a plain HTTP
    GET and clients get "500 / ASGI callable returned without starting response".
    """
    try:
        import websockets  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        import wsproto  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    print(
        "\n*** WARNING: no WebSocket library found (websockets/wsproto). ***\n"
        "The collab server will start but every connection will fail with a 500.\n"
        "Fix it with:  pip install websockets\n",
        file=sys.stderr,
    )


def main() -> int:
    if not os.path.isdir(_WHEELS):
        print("vendor/collab-wheels not found next to this script", file=sys.stderr)
        return 2

    py = "{}.{}".format(sys.version_info[0], sys.version_info[1])
    if _has_matching_pycrdt_wheel() and _has_vendored_ws():
        # Fully offline: every dependency (incl. pycrdt AND a ws lib) is vendored.
        print("Matching pycrdt wheel found for Python {} ({}); offline install."
              .format(py, _platform_fragment()))
        rc = _run([sys.executable, "-m", "pip", "install",
                   "--no-index", "--find-links", _WHEELS, "-r", _REQ])
        if rc != 0:
            print("\nOffline install failed. Try online:\n"
                  "    pip install -r requirements-collab.txt", file=sys.stderr)
        else:
            _verify_ws()
        return rc

    if _has_matching_pycrdt_wheel():
        # pycrdt is vendored but no WebSocket wheel is — pip needs the network for
        # `websockets` only. Prefer vendored wheels, allow PyPI for the rest.
        print("Matching pycrdt wheel found for Python {} ({}), but no vendored "
              "WebSocket lib; installing (vendored wheels + PyPI for websockets)."
              .format(py, _platform_fragment()))
        rc = _run([sys.executable, "-m", "pip", "install",
                   "--find-links", _WHEELS, "-r", _REQ])
        if rc != 0:
            print("\nInstall failed. For a FULLY offline install, vendor a "
                  "WebSocket wheel too:\n"
                  "    pip download 'websockets>=12,<14'\n"
                  "    # copy the .whl into vendor/collab-wheels/ and re-run",
                  file=sys.stderr)
        else:
            _verify_ws()
        return rc

    # Hybrid: prefer vendored wheels, but let pip fetch the right pycrdt binary.
    print("No vendored pycrdt binary matches Python {} on {}; using HYBRID "
          "install (vendored wheels + PyPI for pycrdt)."
          .format(py, _platform_fragment()))
    rc = _run([sys.executable, "-m", "pip", "install",
               "--find-links", _WHEELS, "-r", _REQ])
    if rc == 0:
        _verify_ws()
    if rc != 0:
        print("\nHybrid install failed (offline or no network?).\n"
              "Options:\n"
              "  1) Online:  pip install -r requirements-collab.txt\n"
              "  2) Offline: download a matching wheel on a same-OS/Python box:\n"
              "         pip download pycrdt==0.14.1\n"
              "     copy it into vendor/collab-wheels/ and re-run this script.",
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
