"""Per-category (テスト区分) reviewer routing.

Why this exists
---------------
A project used to have exactly one ``default_reviewer_id``: every automatic
review request in the project landed in one person's queue. That is not how the
work is actually divided. A test matrix is partitioned by テスト区分, and each
区分 belongs to a different feature owner -- the person who can judge a 区分 5
verdict usually has no basis for judging 区分 12. Routing everything to one
reviewer produces either a bottleneck or, more often, rubber-stamping.

So the project now carries an ordered list of routing rules, and the single
default reviewer becomes the fallback for 区分 nobody claimed.

Shape
-----
``Project.review_routes`` is a JSON list, in priority order::

    [{"category": "5",   "reviewer_id": 7},
     {"category": "1*",  "reviewer_id": 3},
     {"category": "ECU", "reviewer_id": 9}]

Design decisions
----------------
* **Ordered list, first match wins.** Order is visible and editable in the UI,
  so "which rule applied?" is answerable by reading the list top to bottom.
  Implicit precedence (longest pattern, most specific glob) is unpredictable
  exactly when two rules overlap, which is the only time precedence matters.
* **Trailing ``*`` only.** ``1*`` covers 1, 10, 19. Full glob/regex would buy
  little on a field that is normally a small integer and would turn a typo into
  a rule that silently matches everything.
* **Category values are normalised, not compared raw.** テスト区分 arrives from
  Excel as ``1``, ``1.0``, ``"01"`` or ``" 1 "`` depending on the cell format. A
  rule typed as ``1`` must match all of them, otherwise the rule looks broken
  for reasons invisible in the UI.
* **Pure module.** No Flask, no SQLAlchemy: the matching rules are the part
  worth testing exhaustively, and they should be testable without a database.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

#: Upper bound on stored rules. A project with more than this many routes has a
#: structural problem the UI cannot help with, and an unbounded list is a way to
#: put arbitrary data in a config column.
MAX_ROUTES = 100

#: Field key holding テスト区分 on a row (``custom_values``, not a column).
CATEGORY_KEY = "category"
#: Field key holding テスト区分名, used for display only.
CATEGORY_NAME_KEY = "category_name"


def normalise_category(raw: Any) -> str:
    """Canonical string form of a テスト区分 value.

    Numeric categories collapse to their plain integer form (``1.0``, ``"01"``
    and ``" 1 "`` all become ``"1"``), everything else is stripped text. Empty
    input yields ``""``, which never matches a rule -- an uncategorised row
    falls through to the project default reviewer rather than being captured by
    whichever rule happens to be first.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):  # bool is an int subclass; not a category
        return ""
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        return str(int(raw)) if raw.is_integer() else str(raw)
    text = str(raw).strip()
    if not text:
        return ""
    # "01" / "1.0" from Excel cells are the same 区分 as "1".
    try:
        as_float = float(text)
    except ValueError:
        return text
    return str(int(as_float)) if as_float.is_integer() else text


def _normalise_pattern(raw: Any) -> str:
    """Canonical form of a rule's category pattern (keeps a trailing ``*``)."""
    text = "" if raw is None else str(raw).strip()
    if text.endswith("*"):
        # Only the stem is normalised: "01*" and "1*" are the same prefix rule.
        stem = normalise_category(text[:-1])
        return f"{stem}*"
    return normalise_category(text)


def matches(pattern: Any, category: Any) -> bool:
    """Whether ``pattern`` covers ``category``.

    Both sides are normalised here as well as at storage time, so the function
    behaves the same whether it is handed canonical stored rules or raw text
    straight from an input box.
    """
    pat = _normalise_pattern(pattern)
    key = normalise_category(category)
    if not pat or not key:
        return False
    if pat == "*":
        return True
    if pat.endswith("*"):
        return key.lower().startswith(pat[:-1].lower())
    return pat.lower() == key.lower()


def normalise_routes(raw: Any) -> list[dict]:
    """Validate/clean stored or submitted rules into a canonical list.

    Silently drops entries that cannot mean anything (no category, no reviewer,
    non-integer reviewer) and the second occurrence of a duplicated pattern: a
    shadowed rule is not an error the user can act on, it is simply dead, and
    keeping it would make the list lie about what happens.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pattern = _normalise_pattern(entry.get("category"))
        if not pattern or pattern in seen:
            continue
        try:
            reviewer_id = int(entry.get("reviewer_id") or 0)
        except (TypeError, ValueError):
            continue
        if reviewer_id <= 0:
            continue
        seen.add(pattern)
        out.append({"category": pattern, "reviewer_id": reviewer_id})
        if len(out) >= MAX_ROUTES:
            break
    return out


def match_reviewer(routes: Iterable[dict], category: Any) -> Optional[int]:
    """Reviewer id for ``category`` under ``routes``, or ``None``.

    ``routes`` is expected to be normalised already (see
    :func:`normalise_routes`); it is re-normalised defensively because the value
    comes from a JSON column that older rows may have written by hand.
    """
    key = normalise_category(category)
    if not key:
        return None
    for rule in normalise_routes(list(routes or [])):
        if matches(rule["category"], key):
            return rule["reviewer_id"]
    return None


def row_category(row: Any) -> str:
    """The normalised テスト区分 of a row, via its field accessor."""
    if row is None:
        return ""
    getter = getattr(row, "get_field", None)
    if callable(getter):
        return normalise_category(getter(CATEGORY_KEY))
    if isinstance(row, dict):
        return normalise_category(row.get(CATEGORY_KEY))
    return ""
