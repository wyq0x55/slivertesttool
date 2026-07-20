"""Force-terminate leftover Silver OS processes on shutdown.

Each open Silver instance holds one license. The pool disposes its instances
gracefully on a clean exit, but an abrupt shutdown -- most notably the all-in-one
launcher killing the worker child on Windows (``TerminateProcess`` does not run
the child's ``atexit`` hooks) -- can leave orphaned Silver processes running and
keep occupying licenses. This module provides a best-effort OS-level sweep that
kills any remaining Silver processes so that closing the app closes *all* Silver.

The sweep is deliberately dependency-free (no ``psutil``): it shells out to the
platform's process tools. It is a safety net, so every failure is swallowed.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger("silver.cleanup")

DEFAULT_IMAGE_NAMES = ["silver.exe", "silver64.exe", "SilverSim.exe"]


def force_kill_silver_processes(image_names: list[str] | None = None) -> int:
    """Force-kill every running Silver process by image name.

    Returns the number of image names for which a kill was attempted. Safe to
    call multiple times and on any platform; unknown platforms are a no-op.
    """
    names = [n for n in (image_names or DEFAULT_IMAGE_NAMES) if n]
    if not names:
        return 0

    attempted = 0
    if sys.platform.startswith("win"):
        for name in names:
            try:
                # /F force, /T also kill child process tree, /IM by image name.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                attempted += 1
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.exception("taskkill failed for image %s", name)
    else:
        # POSIX best effort (Silver targets Windows, but keep dev parity).
        for name in names:
            base = name[:-4] if name.lower().endswith(".exe") else name
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", base],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                attempted += 1
            except FileNotFoundError:
                break  # no pkill available; give up quietly
            except Exception:  # noqa: BLE001
                logger.exception("pkill failed for %s", base)
    if attempted:
        logger.info("Silver exit sweep: attempted to kill %d image name(s): %s",
                    attempted, ", ".join(names))
    return attempted
