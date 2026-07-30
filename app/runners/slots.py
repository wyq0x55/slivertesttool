"""Bounded, reusable integer slot allocator for run-directory labels.

Every executing test needs a private runtime directory under ``POOL_DIR``. The
old scheme labelled that directory with an ever-growing token
(``inst_<uid>`` where ``uid`` came from ``itertools.count``, or
``inst_dedicated_<task_id>``), so a fresh folder was created for every run and
they piled up without bound.

This allocator hands out the *smallest* free positive integer and recycles a
number the moment it is released. Because at most ``N`` slots can be live at
once (``N`` = the peak concurrency, i.e. the licensed pool size), the labels
stay bounded to ``inst_1 .. inst_N`` and their directories are reused instead of
accumulating one-per-task.

The module imports only the standard library so it can be shared by both the
pooled and the dedicated execution paths (and unit-tested in isolation).
"""

from __future__ import annotations

import heapq
import threading
from typing import List


class SlotAllocator:
    """Allocate the lowest free 1-based slot, reusing released numbers.

    Thread-safe: several worker threads may acquire/release concurrently. The
    allocator never calls back into its callers while holding the lock, so it is
    safe to nest inside another lock (e.g. the pool's condition variable).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._free: List[int] = []   # min-heap of released slot numbers
        self._high = 0               # highest slot ever handed out

    def acquire(self) -> int:
        """Return the smallest currently-free slot number (>= 1)."""
        with self._lock:
            if self._free:
                return heapq.heappop(self._free)
            self._high += 1
            return self._high

    def release(self, slot: int) -> None:
        """Return *slot* to the free pool so it can be reused."""
        try:
            n = int(slot)
        except (TypeError, ValueError):
            return
        if n < 1:
            return
        with self._lock:
            # Guard against a double release leaving duplicates in the heap.
            if n not in self._free:
                heapq.heappush(self._free, n)

    def peak(self) -> int:
        """Highest slot number ever allocated (== bound on live folders)."""
        with self._lock:
            return self._high


# --------------------------------------------------------------------------- #
# Process-wide allocator for the *dedicated* (non-pooled) execution path.
# --------------------------------------------------------------------------- #
_dedicated = SlotAllocator()


def dedicated_allocator() -> SlotAllocator:
    """Return the shared allocator used to label dedicated (non-pooled) runs."""
    return _dedicated
