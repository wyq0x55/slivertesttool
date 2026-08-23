"""Semantic signal registry — path ↔ 表示名, assembled without human upkeep.

The registry is what lets prompts carry semantics while model output carries
only canonical paths. Every source is a by-product of work that already
exists; nothing here is hand-maintained:

    clang declaration comments  (new project day one — code documents itself)
    SBS text mining             (once the sbs scenario has bootstrapped it)
    historical procedure rows   (approved steps' signal headers, aggregated;
                                  grows monotonically with project use)
    viewpoint seeds             (design-doc terms paired to code names during
                                  viewpoint extraction)
    project signal dictionary   (optional, human-curated via the AI settings
                                  panel; the only intentional manual source)

Merge priority — later wins because it is closer to how people actually refer
to the signal in this project: clang comment < sbs < viewpoint seed <
historical rows < signal dictionary. The winning display name and its origin
are kept so the review UI can flag "AI-proposed" names separately.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

_PRIO_COMMENT = 0
_PRIO_SBS = 1
_PRIO_SEED = 2
_PRIO_HISTORY = 3
_PRIO_DICT = 4

Entry = dict[str, Any]  # {"display": str, "type": str, "source": str}


class Registry:
    """Path-keyed table with tolerant name resolution for sparse output."""

    def __init__(self, entries: Optional[dict[str, Entry]] = None):
        # canonical path -> entry
        self._entries: dict[str, Entry] = dict(entries or {})

    # ------------------------------------------------------------------ #
    def add(self, path: str, display: str, *, source: str, prio: int,
            type_: str = "") -> None:
        path = (path or "").strip()
        display = (display or "").strip()
        if not path:
            return
        current = self._entries.get(path)
        if current is not None:
            # An empty display (e.g. a bare-path seed) must never wipe a
            # display a better source already provided — it can only fill
            # gaps: type, or the display when none exists yet.
            if not display:
                if type_ and not current.get("type"):
                    current["type"] = type_
                return
            if prio >= current["_prio"]:
                current.update({
                    "display": display or path,
                    "type": type_ or current.get("type", ""),
                    "source": source,
                    "_prio": prio,
                })
            return
        self._entries[path] = {
            "display": display or path,
            "type": type_,
            "source": source,
            "_prio": prio,
        }

    def __contains__(self, path: str) -> bool:
        return path in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def paths(self) -> list[str]:
        return sorted(self._entries)

    def display(self, path: str) -> str:
        entry = self._entries.get(path)
        return entry["display"] if entry else path

    def type_of(self, path: str) -> str:
        entry = self._entries.get(path)
        return entry["type"] if entry else ""

    # ------------------------------------------------------------------ #
    def resolve(self, name: str) -> str:
        """Map a name used in sparse output to its canonical path.

        Resolution order: exact path, exact 表示名, unique last-segment match.
        Raises ``KeyError`` for unknown names and ``ValueError`` when a short
        name is ambiguous — ambiguity must fail loudly, never guess.
        """
        name = (name or "").strip()
        if not name:
            raise KeyError("")
        if name in self._entries:
            return name
        by_display = [p for p, e in self._entries.items()
                      if e["display"] == name]
        if len(by_display) == 1:
            return by_display[0]
        if len(by_display) > 1:
            raise ValueError(f"表示名 {name!r} 对应多个信号：{by_display}")
        tail = name.rsplit(".", 1)[-1]
        by_tail = [p for p in self._entries if p.rsplit(".", 1)[-1] == tail]
        if len(by_tail) == 1:
            return by_tail[0]
        if len(by_tail) > 1:
            raise ValueError(f"名字 {name!r} 歧义，请用完整路径：{by_tail}")
        raise KeyError(name)

    # ------------------------------------------------------------------ #
    def prompt_lines(self) -> list[str]:
        """``路径（表示名）: 类型`` — the semantic inventory for prompts."""
        lines = []
        for path in self.paths():
            entry = self._entries[path]
            type_part = f": {entry['type']}" if entry["type"] else ""
            lines.append(f"{path}（{entry['display']}）{type_part}")
        return lines


# --------------------------------------------------------------------------- #
# Source extractors
# --------------------------------------------------------------------------- #
def from_index(index: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Extract ``{path: (display, type)}`` from a :mod:`c_index` result.

        Paths are bare variable names qualified by file when the same name
        appears in several files (the Silver model qualifies them anyway).
    """
    out: dict[str, tuple[str, str]] = {}
    name_files: dict[str, list[str]] = {}
    for fname, entry in index.get("files", {}).items():
        for g in entry.get("globals", []):
            name_files.setdefault(g["name"], []).append(fname)
    for fname, entry in index.get("files", {}).items():
        for g in entry.get("globals", []):
            path = g["name"] if len(set(name_files[g["name"]])) == 1 \
                else f"{fname}.{g['name']}"
            out.setdefault(path, (g.get("comment") or "", g.get("type") or ""))
    return out


_QUOTED_RE = re.compile(r'"([^"\n]{1,80})"|/\*+\s*([^*\n]{1,80}?)\s*\*+/|//+\s*(.{1,80})$')


def from_sbs_text(sbs_text: str, known_names: Iterable[str]) -> dict[str, str]:
    """Mine ``{path: display}`` from SBS text.

    Tolerant by design (SBS dialects vary): for each line declaring a known
    variable, take a quoted label or a trailing comment on that line.
    """
    out: dict[str, str] = {}
    names = set(known_names)
    if not sbs_text:
        return out
    for line in sbs_text.splitlines():
        hit = next((n for n in names if re.search(
                r"\b" + re.escape(n) + r"\b", line)), None)
        if hit is None:
            continue
        match = _QUOTED_RE.search(line)
        if not match:
            continue
        label = next((g for g in match.groups() if g), "")
        label = label.strip()
        if label and label != hit:
            out[hit] = label
    return out


def from_history(pairs: Iterable[Any]) -> dict[str, str]:
    """Aggregate ``{path: display}`` from historical signal-header pairs.

    Input: iterable of ``[display, path]`` (or ``(display, path)``) taken
    from approved procedures' ``input_signals``/``expected_signals`` headers.
    The most frequent non-empty display per path wins.
    """
    counts: dict[str, dict[str, int]] = {}
    for pair in pairs or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        display, path = str(pair[0] or "").strip(), str(pair[1] or "").strip()
        if not path or not display or display == path:
            continue
        counts.setdefault(path, {})
        counts[path][display] = counts[path].get(display, 0) + 1
    return {path: max(votes, key=votes.get) for path, votes in counts.items()}


def build(*,
          index: Optional[dict[str, Any]] = None,
          sbs_text: str = "",
          sbs_variables: Optional[list[Any]] = None,
          historical_pairs: Optional[Iterable[Any]] = None,
          viewpoint_seeds: Optional[Iterable[Any]] = None,
          signal_dict: Optional[Iterable[Any]] = None,
          extra_types: Optional[dict[str, str]] = None) -> Registry:
    """Assemble the registry from every available source.

    ``sbs_variables``: ``[["表示名","路径"], ...]`` pairs (bare ``"path"``
    strings are tolerated — they register the path with a degraded display).
    ``viewpoint_seeds``: same pair shape, from the viewpoint scenario output.
    ``signal_dict``: ``[["表示名","路径","类型"], ...]`` triples (type
    optional) from the project's curated dictionary — highest priority, it
    is the one source a human deliberately maintains.
    """
    reg = Registry()
    for path, (display, type_) in from_index(index or {}).items():
        reg.add(path, display, source="comment", prio=_PRIO_COMMENT,
                type_=type_)
    for path, display in from_sbs_text(sbs_text, reg.paths()).items():
        reg.add(path, display, source="sbs", prio=_PRIO_SBS)

    def _add_pairs(pairs, source, prio):
        for pair in pairs or []:
            if isinstance(pair, str):
                reg.add(pair, "", source=source, prio=prio)
            elif isinstance(pair, (list, tuple)) and len(pair) in (2, 3):
                type_ = str(pair[2]) if len(pair) == 3 and pair[2] else ""
                reg.add(str(pair[1]), str(pair[0]), source=source,
                        prio=prio, type_=type_)

    _add_pairs(sbs_variables, "sbs", _PRIO_SBS)
    _add_pairs(viewpoint_seeds, "seed", _PRIO_SEED)
    for path, display in from_history(historical_pairs or []).items():
        reg.add(path, display, source="history", prio=_PRIO_HISTORY)
    _add_pairs(signal_dict, "dict", _PRIO_DICT)
    for path, type_ in (extra_types or {}).items():
        entry = reg._entries.get(path)  # noqa: SLF001 - same module family
        if entry is not None and not entry.get("type"):
            entry["type"] = type_
    return reg
