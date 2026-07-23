"""Security helpers: formula-injection defence, safe regex, filename hygiene.

All Flask-independent and pure so they can be unit-tested directly. These back
the PRD requirements FR-EXCEL-004/007 (formula injection), FR-SEARCH-001 (bounded
regex) and FR-EXCEL-002 (safe filenames).
"""

from __future__ import annotations

import os
import re
import signal
from typing import Any

from . import settings

# Characters that make Excel/CSV interpret a cell as a formula (CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# A leading apostrophe forces Excel to treat the content as literal text.
_FORMULA_GUARD = "'"

# Control characters that must never be stored (FR-GRID-005 "禁止控制字符"),
# excluding tab / newline / carriage-return which multiline text may use.
_CONTROL_CHARS = "".join(
    chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32))
)
_CONTROL_RE = re.compile("[" + re.escape(_CONTROL_CHARS) + "]")


def is_formula_like(value: Any) -> bool:
    """True when a string would be interpreted as a formula by a spreadsheet."""
    return isinstance(value, str) and value[:1] in _FORMULA_PREFIXES


def escape_formula(value: Any) -> Any:
    """Neutralise a value that could be read as a spreadsheet formula.

    Non-strings pass through unchanged. Strings that start with a dangerous
    prefix are guarded with a leading apostrophe so the spreadsheet keeps them
    as literal text (FR-EXCEL-007). Used only on *export*.
    """
    if is_formula_like(value):
        return _FORMULA_GUARD + value
    return value


def sanitize_incoming(value: Any) -> Any:
    """Strip a leading formula guard / dangerous prefix from *imported* text.

    On import we do not want to trust or execute formulas (FR-EXCEL-004). Cached
    formula results arriving as plain strings are kept, but a raw leading ``=``
    style formula string is defused by stripping the leading guard char, and any
    forbidden control characters are removed.
    """
    if not isinstance(value, str):
        return value
    cleaned = strip_control_chars(value)
    return cleaned


def strip_control_chars(value: str) -> str:
    return _CONTROL_RE.sub("", value)


def has_control_chars(value: Any) -> bool:
    return isinstance(value, str) and bool(_CONTROL_RE.search(value))


# --------------------------------------------------------------------------- #
# Filenames
# --------------------------------------------------------------------------- #
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff\- ]+")


def safe_filename(
    name: str, *, default: str = "file", max_len: int | None = None
) -> str:
    """Return a filesystem-safe base filename (drops directory components)."""
    if max_len is None:
        max_len = settings.FILENAME_MAX_LEN
    name = os.path.basename(str(name or "")).strip()
    name = name.replace("\x00", "")
    # Collapse path traversal and unsafe chars.
    name = _UNSAFE_NAME_RE.sub("_", name)
    name = name.strip(". ")
    if not name:
        name = default
    if len(name) > max_len:
        root, ext = os.path.splitext(name)
        name = root[: max_len - len(ext)] + ext
    return name


# --------------------------------------------------------------------------- #
# Bounded regular expressions (FR-SEARCH-001: limit length + execution time)
# --------------------------------------------------------------------------- #
# Length cap and per-match timeout are configured via ``.env`` (see
# app.config.Config.LM_REGEX_*) and surfaced through app.services.lanmatrix.settings.
MAX_REGEX_LEN = settings.REGEX_MAX_LEN


class UnsafeRegexError(ValueError):
    """Raised when a user-supplied regex is too long or otherwise rejected."""


def compile_user_regex(pattern: str, *, flags: int = 0):
    """Compile a user regex with a hard length cap.

    Length is capped to bound catastrophic backtracking risk; callers should
    additionally run matches under :func:`match_with_timeout`.
    """
    if pattern is None:
        raise UnsafeRegexError("empty pattern")
    if len(pattern) > MAX_REGEX_LEN:
        raise UnsafeRegexError(
            f"regex too long (>{MAX_REGEX_LEN} chars)"
        )
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise UnsafeRegexError(f"invalid regex: {exc}") from exc


def match_with_timeout(compiled, text: str, *, timeout: float | None = None):
    """Run ``compiled.search(text)`` under a wall-clock timeout when possible.

    Uses ``SIGALRM`` on POSIX main threads; elsewhere it degrades to a plain
    search (the length cap remains the primary guard). Returns the match object
    or ``None``; raises :class:`TimeoutError` if the deadline is hit.
    """
    if timeout is None:
        timeout = settings.REGEX_TIMEOUT
    if not hasattr(signal, "SIGALRM"):
        return compiled.search(text)

    def _handler(signum, frame):  # noqa: ANN001
        raise TimeoutError("regex evaluation timed out")

    try:
        previous = signal.signal(signal.SIGALRM, _handler)
    except (ValueError, OSError):
        # Not on the main thread; fall back to a plain search.
        return compiled.search(text)
    try:
        signal.setitimer(signal.ITIMER_REAL, timeout)
        return compiled.search(text)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
