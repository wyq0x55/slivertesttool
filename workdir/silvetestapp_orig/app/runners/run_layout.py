"""Filesystem layout for test runs, keyed by *project name* + *test id*.

The platform no longer identifies a run's directories by the synthetic
``task_key`` (``T000022``). Instead:

* **Results** (the persistent ``测试结果``) live under
  ``WORKSPACE_DIR/<project name>/log/<test id>/`` — one folder per test id,
  reused (overwritten) on every re-run.
* **Run scripts** are materialised into a short-lived *staging* area at enqueue
  time, copied into the *runtime* pool-instance directory
  (``POOL_DIR/<inst label>/run_<test id>/``) just before execution, and
  **deleted** once the run finishes.

``task.workspace`` stores the project root (``WORKSPACE_DIR/<project name>``)
and is therefore shared by every test id in the project — callers that clean up
a single task must only remove that test id's subtree, never the whole root.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Characters that are unsafe as a single path segment on common filesystems
# (Windows-reserved plus control chars and the path separators). Unicode letters
# (incl. CJK/kana used in Japanese project names) are preserved.
_BAD_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
# Test-id token used for the ``log``/staging/run folder names.
_SAFE_TID = re.compile(r"[^0-9A-Za-z._\-]+")


def sanitize_segment(name: str, fallback: str) -> str:
    """A filesystem-safe single path segment derived from *name*."""
    token = _BAD_SEGMENT.sub("_", (name or "").strip())
    token = token.strip(" .")
    return token or fallback


def safe_tid(test_id: str) -> str:
    """A filesystem-safe token for a test id (used as a folder name)."""
    token = _SAFE_TID.sub("_", (test_id or "").strip()).strip("_")
    return token or "testcase"


def project_root(config, project) -> Path:
    """The persistent per-project root: ``WORKSPACE_DIR/<project name>``."""
    seg = sanitize_segment(getattr(project, "name", "") or "",
                           f"project_{getattr(project, 'id', 'x')}")
    return Path(config.WORKSPACE_DIR) / seg


def log_dir(workspace_root, test_id: str) -> Path:
    """Persistent results dir for a test id under a project root."""
    return Path(workspace_root) / "log" / safe_tid(test_id)


def staging_dir(workspace_root, test_id: str) -> Path:
    """Short-lived dir (outside ``log/``) where run scripts are materialised at
    enqueue time; it holds the ``<raw test id>/`` scripts folder."""
    return Path(workspace_root) / ".pending" / safe_tid(test_id)


def instance_label(task_id: int, instance: Any = None) -> str:
    """Directory label for the runtime instance a job executes on."""
    uid = getattr(instance, "uid", None)
    if uid is not None:
        return f"inst_{uid}"
    return f"inst_dedicated_{task_id}"


def instance_run_dir(config, task_id: int, test_id: str, instance: Any = None) -> Path:
    """Runtime run-script dir under ``POOL_DIR`` for the chosen instance.

    Scripts are copied here just before execution and removed afterwards.
    """
    return (Path(config.POOL_DIR) / instance_label(task_id, instance)
            / f"run_{safe_tid(test_id)}")
