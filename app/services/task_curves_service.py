"""Exact, indexed access to a Silver task's CsvWriter ``output.csv``.

Silver's CsvWriter (``-w 0 -I 0.001 -m t``) writes one fixed format: a quoted
header whose first column is ``time`` (seconds), then one comma-separated
record per sample. Most columns are discrete state signals that change a few
times per run, so the file is parsed once into a per-column *change-point*
index (row of each value change plus the new value). Every query answers from
that index exactly; only a signal with more change points than the configured
limit in the requested range is sent as a per-pixel min/max envelope, and the
response says so.

The index is cached on disk keyed by the artifact revision (task, attempt and
file identity), so zooming, cursors and event lists never re-scan the CSV.
Source files are never modified.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import heapq
import io
import json
import math
import os
import struct
import threading
from array import array
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from . import run_evidence_service as evidence

MAX_COLUMNS = 1024
MAX_ROWS = 5_000_000
MAX_LINE = 1 << 20
RAW_PAGE = 100
EVENT_PAGE = 500
FORMAT_VERSION = 2
_MAGIC = b"SCIDX2\n"


class CurveError(ValueError):
    def __init__(self, message, code="CSV_INVALID", status=422):
        super().__init__(message)
        self.code, self.status = code, status


# --------------------------------------------------------------------------- #
# Settings (admin console adjustable)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Limits:
    max_bytes: int
    point_limit: int
    max_lanes: int
    cache_bytes: int


def limits() -> Limits:
    from ..config import Config
    try:
        from flask import has_app_context
        live = has_app_context()
    except ImportError:  # pragma: no cover
        live = False
    if live:
        from . import runtime_config as rc
        values = {key: rc.get_int(key) for key in
                  ("curve_max_file_mb", "curve_point_limit", "curve_max_lanes", "curve_cache_mb")}
    else:
        values = {"curve_max_file_mb": Config.CURVE_MAX_FILE_MB, "curve_point_limit": Config.CURVE_POINT_LIMIT,
                  "curve_max_lanes": Config.CURVE_MAX_LANES, "curve_cache_mb": Config.CURVE_CACHE_MB}
    return Limits(values["curve_max_file_mb"] << 20, values["curve_point_limit"],
                  values["curve_max_lanes"], values["curve_cache_mb"] << 20)


def cache_dir() -> Path:
    from ..config import Config
    try:
        from flask import current_app, has_app_context
        if has_app_context() and current_app.config.get("CURVE_CACHE_DIR"):
            return Path(current_app.config["CURVE_CACHE_DIR"])
    except ImportError:  # pragma: no cover
        pass
    return Path(Config.CURVE_CACHE_DIR)


# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #
@dataclass
class Source:
    task: object
    run_count: int
    path: Path
    revision: str
    live: bool = False


def _revision(task, run_count, path, live):
    try:
        info = path.stat()
    except OSError as exc:
        raise CurveError("The task artifact is no longer available; reload", "CSV_CHANGED", 409) from exc
    identity = (task.id, run_count, live, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return hashlib.sha256(repr(identity).encode()).hexdigest()[:24]


def attempts(task):
    """Attempts the viewer can open: sealed ones, or the single legacy result."""
    sealed = evidence.sealed_attempts(task)
    return sealed or [task.run_count or 1]


def _live_path(task, run_count):
    """The output.csv Silver is writing for a running attempt (pool run dir)."""
    from ..config import Config
    try:
        from flask import current_app, has_app_context
        pool = Path(current_app.config.get("POOL_DIR") or Config.POOL_DIR) if has_app_context() else Path(Config.POOL_DIR)
    except ImportError:  # pragma: no cover
        pool = Path(Config.POOL_DIR)
    key = evidence.validate_segment(task.task_key)
    for run_dir in sorted(pool.glob(f"*/run_{key}_{run_count}")):
        path = evidence.checked_path(pool, run_dir / ".results" / "output.csv")
        if path.is_file():
            return path
    raise CurveError("CsvWriter has not written output.csv for the running attempt yet",
                     "CSV_PENDING", 404)


def resolve(task, run_count=None, *, live=False, expected_revision=None):
    """Bind a request to one attempt's artifact; never to a caller path."""
    current = task.run_count or 1
    count = current if run_count is None else run_count
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= current:
        raise CurveError("Unknown attempt", "CSV_OPTIONS", 400)
    lim = limits()
    if live:
        if count != current or getattr(task, "status", "") != "running":
            raise CurveError("Only the running attempt has a live output.csv", "CSV_OPTIONS", 400)
        path = _live_path(task, count)
        if path.stat().st_size > lim.max_bytes:
            raise CurveError("output.csv exceeds the configured size limit", "CSV_TOO_LARGE", 413)
    else:
        try:
            path = evidence.result_file(task, "output.csv", max_bytes=lim.max_bytes, run_count=count)
        except (OSError, ValueError) as exc:
            if "size limit" in str(exc):
                raise CurveError("output.csv exceeds the configured size limit; download the run report",
                                 "CSV_TOO_LARGE", 413) from exc
            raise CurveError("No readable, verified output.csv for this attempt",
                             "CSV_UNAVAILABLE", 404) from exc
    revision = _revision(task, count, path, live)
    if expected_revision and expected_revision != revision:
        raise CurveError("The task artifact changed; reload", "CSV_CHANGED", 409)
    return Source(task, count, path, revision, live)


# --------------------------------------------------------------------------- #
# Parsing into a change-point index
# --------------------------------------------------------------------------- #
def _lines(stream, max_bytes, *, partial_tail):
    """Decoded physical lines with a read budget; a live file may end mid-line."""
    budget = max_bytes
    pending = b""
    while True:
        chunk = stream.read(min(1 << 20, budget + 1))
        if not chunk:
            break
        budget -= len(chunk)
        if budget < 0:
            raise CurveError("output.csv exceeds the configured size limit", "CSV_TOO_LARGE", 413)
        pending += chunk
        *complete, pending = pending.split(b"\n")
        for line in complete:
            if len(line) > MAX_LINE:
                raise CurveError("output.csv line exceeds 1 MiB", "CSV_TOO_LARGE", 413)
            yield line
        if len(pending) > MAX_LINE:
            raise CurveError("output.csv line exceeds 1 MiB", "CSV_TOO_LARGE", 413)
    if pending and not partial_tail:
        yield pending


def _decode(line, number):
    try:
        return line.decode("utf-8-sig" if number == 1 else "utf-8").rstrip("\r")
    except UnicodeDecodeError as exc:
        raise CurveError(f"output.csv record {number} is not UTF-8/ASCII text", "CSV_FORMAT") from exc


def _float(text):
    try:
        value = float(text)
    except (ValueError, OverflowError):
        return math.nan
    return value if math.isfinite(value) else math.nan


class Column:
    __slots__ = ("index", "name", "rows", "values", "distinct", "low", "high",
                 "integer", "gaps")

    def __init__(self, index, name):
        self.index, self.name = index, name
        self.rows = array("I")
        self.values = array("d")
        self.distinct: set | None = set()
        self.low = math.inf
        self.high = -math.inf
        self.integer = True
        self.gaps = 0

    @property
    def id(self):
        return f"c{self.index}"

    @property
    def changes(self):
        return max(0, len(self.rows) - 1)

    def kind(self):
        if self.changes == 0:
            return "constant"
        if self.integer and self.distinct is not None and len(self.distinct) <= 32:
            return "discrete"
        return "analog"


class Index:
    """Per-column change points over a shared time array."""

    def __init__(self, revision, header, times, columns, live=False):
        self.revision, self.header, self.times, self.columns = revision, header, times, columns
        self.live = live

    # -- construction ----------------------------------------------------- #
    @classmethod
    def parse(cls, source: Source, max_bytes: int) -> "Index":
        with source.path.open("rb") as stream:
            lines = _lines(stream, max_bytes, partial_tail=source.live)
            try:
                first = next(lines)
            except StopIteration:
                raise CurveError("output.csv is empty", "CSV_EMPTY") from None
            header = next(csv.reader([_decode(first, 1)]))
            if not header or header[0].strip().lower() != "time":
                raise CurveError("Not a CsvWriter output.csv: the first column must be \"time\"",
                                 "CSV_FORMAT")
            if not 2 <= len(header) <= MAX_COLUMNS:
                raise CurveError(f"output.csv must have 2 to {MAX_COLUMNS} columns", "CSV_FORMAT")
            columns = [Column(i, name) for i, name in enumerate(header) if i]
            times = array("d")
            last_text = [None] * len(header)
            width = len(header)
            last_time = -math.inf
            for number, raw in enumerate(lines, 2):
                if not raw.strip():
                    continue
                if number - 1 > MAX_ROWS:
                    raise CurveError("output.csv exceeds the record limit", "CSV_TOO_LARGE", 413)
                line = _decode(raw, number)
                cells = line.split(",") if '"' not in line else next(csv.reader([line]))
                if len(cells) != width:
                    raise CurveError(f"output.csv record {number} has {len(cells)} fields, expected {width}",
                                     "CSV_FORMAT")
                time = _float(cells[0])
                if math.isnan(time) or time < last_time:
                    raise CurveError(f"output.csv record {number} has an invalid or decreasing time",
                                     "CSV_FORMAT")
                last_time = time
                row = len(times)
                times.append(time)
                for column in columns:
                    text = cells[column.index]
                    if text == last_text[column.index]:
                        continue
                    last_text[column.index] = text
                    value = _float(text.strip())
                    previous = column.values[-1] if column.values else None
                    if previous is not None and (previous == value or (math.isnan(previous) and math.isnan(value))):
                        continue
                    column.rows.append(row)
                    column.values.append(value)
                    if math.isnan(value):
                        column.gaps += 1
                        continue
                    column.low = min(column.low, value)
                    column.high = max(column.high, value)
                    if column.integer and not value.is_integer():
                        column.integer = False
                    if column.distinct is not None:
                        column.distinct.add(value)
                        if len(column.distinct) > 64:
                            column.distinct = None
        if not times:
            raise CurveError("output.csv has no records", "CSV_NO_DATA")
        return cls(source.revision, header, times, columns, live=source.live)

    # -- disk cache ------------------------------------------------------- #
    def dump(self, path: Path):
        meta = {"version": FORMAT_VERSION, "revision": self.revision, "header": self.header,
                "rows": len(self.times), "columns": []}
        blobs = [self.times.tobytes()]
        for c in self.columns:
            meta["columns"].append({"index": c.index, "n": len(c.rows), "low": _json_number(c.low),
                                    "high": _json_number(c.high), "integer": c.integer, "gaps": c.gaps,
                                    "distinct": None if c.distinct is None else sorted(c.distinct)})
            blobs += [c.rows.tobytes(), c.values.tobytes()]
        head = json.dumps(meta, ensure_ascii=False, allow_nan=False).encode()
        temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with temp.open("wb") as out:
            out.write(_MAGIC + struct.pack("<Q", len(head)) + head)
            for blob in blobs:
                out.write(blob)
        os.replace(temp, path)

    @classmethod
    def load(cls, path: Path, revision: str) -> "Index | None":
        try:
            with path.open("rb") as stream:
                if stream.read(len(_MAGIC)) != _MAGIC:
                    return None
                (size,) = struct.unpack("<Q", stream.read(8))
                meta = json.loads(stream.read(size))
                if meta.get("version") != FORMAT_VERSION or meta.get("revision") != revision:
                    return None
                times = array("d")
                times.frombytes(stream.read(8 * meta["rows"]))
                columns = []
                for info in meta["columns"]:
                    c = Column(info["index"], meta["header"][info["index"]])
                    c.rows.frombytes(stream.read(c.rows.itemsize * info["n"]))
                    c.values.frombytes(stream.read(8 * info["n"]))
                    c.low = math.inf if info["low"] is None else info["low"]
                    c.high = -math.inf if info["high"] is None else info["high"]
                    c.integer, c.gaps = info["integer"], info["gaps"]
                    c.distinct = None if info["distinct"] is None else set(info["distinct"])
                    if len(c.rows) != info["n"] or len(c.values) != info["n"]:
                        return None
                    columns.append(c)
                if len(times) != meta["rows"]:
                    return None
            os.utime(path)  # LRU by modification time
            return cls(revision, meta["header"], times, columns)
        except (OSError, ValueError, KeyError, struct.error):
            return None

    # -- lookup helpers --------------------------------------------------- #
    def column(self, ident) -> Column:
        if (not isinstance(ident, str) or not 2 <= len(ident) <= 5 or not ident.startswith("c")
                or not ident[1:].isascii() or not ident[1:].isdigit()):
            raise CurveError("Choose a valid variable", "CSV_OPTIONS", 400)
        index = int(ident[1:])
        if not 1 <= index <= len(self.columns):
            raise CurveError("Unknown variable", "CSV_OPTIONS", 400)
        return self.columns[index - 1]

    def selection(self, idents, max_lanes):
        if not idents or len(idents) > max_lanes or len(set(idents)) != len(idents):
            raise CurveError(f"Select 1 to {max_lanes} distinct variables", "CSV_OPTIONS", 400)
        return [self.column(i) for i in idents]

    def by_name(self):
        names = {}
        for c in self.columns:
            names.setdefault(c.name, c)
        return names

    def row_at(self, t):
        """Last record whose time is <= t (the sample that holds at t)."""
        return max(0, bisect.bisect_right(self.times, t) - 1)

    def domain(self):
        return [self.times[0], self.times[-1]]

    def span(self, start, end):
        lo, hi = self.domain()
        start = lo if start is None else max(lo, start)
        end = hi if end is None else min(hi, end)
        if start > end:
            raise CurveError("Range start must not exceed end", "CSV_OPTIONS", 400)
        return start, end

    def value_at_row(self, c: Column, row):
        k = bisect.bisect_right(c.rows, row) - 1
        return c.values[k] if k >= 0 else math.nan

    # -- queries ---------------------------------------------------------- #
    def metadata(self, max_lanes):
        times = self.times
        step = None
        if len(times) > 1:
            gaps = sorted(times[i + 1] - times[i] for i in range(min(len(times) - 1, 200)))
            step = gaps[len(gaps) // 2]
        cols = []
        for c in self.columns:
            distinct = None if c.distinct is None else sorted(c.distinct)
            cols.append({"id": c.id, "index": c.index, "name": c.name, "kind": c.kind(),
                         "changes": c.changes, "gaps": c.gaps,
                         "initial": _json_number(c.values[0]) if len(c.values) else None,
                         "min": _json_number(c.low), "max": _json_number(c.high),
                         "integer": c.integer,
                         "values": [_json_number(v) for v in distinct][:32] if distinct is not None else None})
        return {"revision": self.revision, "rows": len(times), "domain": self.domain(),
                "sample_interval": step, "time_unit": "s", "live": self.live,
                "columns": cols, "limits": {"lanes": max_lanes}}

    def series(self, idents, *, start=None, end=None, width=1600, max_lanes=32, point_limit=5000):
        if not 50 <= width <= 8192:
            raise CurveError("Width must be between 50 and 8192 pixels", "CSV_OPTIONS", 400)
        chosen = self.selection(idents, max_lanes)
        start, end = self.span(start, end)
        lo_row, hi_row = self.row_at(start), self.row_at(end)
        out = []
        for c in chosen:
            first = max(0, bisect.bisect_right(c.rows, lo_row) - 1)
            last = bisect.bisect_right(c.rows, hi_row)
            count = last - first
            entry = {"id": c.id, "kind": c.kind()}
            if count <= point_limit:
                entry["mode"] = "exact"
                entry["points"] = [[max(start, self.times[c.rows[k]]), _json_number(c.values[k]), c.rows[k] + 1]
                                   for k in range(first, last)]
            else:
                entry["mode"] = "envelope"
                entry["buckets"] = self._envelope(c, first, last, start, end, width)
            out.append(entry)
        return {"revision": self.revision, "range": [start, end], "domain": self.domain(),
                "series": out}

    def _envelope(self, c, first, last, start, end, width):
        span = (end - start) or 1.0
        buckets = []
        current = None
        for k in range(first, last):
            t = max(start, self.times[c.rows[k]])
            v = c.values[k]
            slot = min(width - 1, int((t - start) / span * width))
            if current is None or current[0] != slot:
                if current is not None:
                    buckets.append(current[1:])
                current = [slot, t, v, v, v, v, False]  # slot, t, first, last, min, max, gap
            current[3] = v
            if math.isnan(v):
                current[6] = True
            else:
                current[4] = v if math.isnan(current[4]) else min(current[4], v)
                current[5] = v if math.isnan(current[5]) else max(current[5], v)
        if current is not None:
            buckets.append(current[1:])
        return [[b[0], _json_number(b[1]), _json_number(b[2]), _json_number(b[3]), _json_number(b[4]), b[5]]
                for b in buckets]

    def values_at(self, idents, t, max_lanes):
        chosen = self.selection(idents, max_lanes)
        row = self.row_at(t)
        return {"revision": self.revision, "t": self.times[row], "row": row + 1,
                "values": {c.id: _json_number(self.value_at_row(c, row)) for c in chosen}}

    def edge(self, ident, t, direction):
        """Time of the next (direction=1) or previous (-1) change of one signal."""
        c = self.column(ident)
        when = lambda k: self.times[c.rows[k]]  # noqa: E731 - nondecreasing in k
        keys = range(1, len(c.rows))
        if direction > 0:
            k = 1 + bisect.bisect_right(keys, t, key=when)
        else:
            k = bisect.bisect_left(keys, t, key=when)
        if not 1 <= k < len(c.rows):
            return {"t": None}
        return {"t": when(k), "row": c.rows[k] + 1,
                "value": _json_number(c.values[k]), "previous": _json_number(c.values[k - 1])}

    def events(self, idents=None, *, start=None, end=None, offset=0, limit=EVENT_PAGE):
        if not 0 <= offset <= MAX_ROWS * 8 or not 1 <= limit <= EVENT_PAGE:
            raise CurveError("Invalid event page", "CSV_OPTIONS", 400)
        chosen = [self.column(i) for i in idents] if idents else [c for c in self.columns if c.changes]
        start, end = self.span(start, end)
        lo_row, hi_row = self.row_at(start), self.row_at(end)

        def stream(c):
            for j in range(max(1, bisect.bisect_left(c.rows, lo_row)), len(c.rows)):
                row = c.rows[j]
                if row > hi_row:
                    return
                if self.times[row] >= start:
                    yield row, c.index, j, c
        items, total = [], 0
        for row, _, j, c in heapq.merge(*(stream(c) for c in chosen)):
            if offset <= total < offset + limit:
                items.append({"t": self.times[row], "row": row + 1, "id": c.id,
                              "from": _json_number(c.values[j - 1]), "to": _json_number(c.values[j])})
            total += 1
        return {"revision": self.revision, "range": [start, end], "offset": offset,
                "total": total, "events": items}

    def divergence(self, other: "Index", idents=None):
        """First time each common (by name) signal differs between two runs."""
        mine, theirs = self.by_name(), other.by_name()
        names = [self.column(i).name for i in idents] if idents else [n for n in mine if n in theirs]
        result = []
        for name in names:
            if name not in theirs:
                continue
            a, b = mine[name], theirs[name]
            at = _first_difference(self.times, a, other.times, b)
            result.append({"id": a.id, "other_id": b.id, "name": name, "t": at})
        diverged = [r for r in result if r["t"] is not None]
        first = min(diverged, key=lambda r: r["t"]) if diverged else None
        return {"revision": self.revision, "other_revision": other.revision,
                "only_here": [mine[n].id for n in mine if n not in theirs],
                "only_there": [theirs[n].id for n in theirs if n not in mine],
                "signals": result, "first": first,
                "domains": [self.domain(), other.domain()]}


def _same(x, y):
    return x == y or (math.isnan(x) and math.isnan(y))


def _first_difference(ta, a, tb, b):
    ia = ib = 0
    va, vb = a.values[0], b.values[0]
    if not _same(va, vb):
        return max(ta[a.rows[0]], tb[b.rows[0]])
    ia = ib = 1
    while ia < len(a.rows) or ib < len(b.rows):
        na = ta[a.rows[ia]] if ia < len(a.rows) else math.inf
        nb = tb[b.rows[ib]] if ib < len(b.rows) else math.inf
        t = min(na, nb)
        while ia < len(a.rows) and ta[a.rows[ia]] == t:
            va = a.values[ia]
            ia += 1
        while ib < len(b.rows) and tb[b.rows[ib]] == t:
            vb = b.values[ib]
            ib += 1
        if not _same(va, vb):
            return t
    return None


def _json_number(value):
    if value is None or isinstance(value, bool):
        return value
    if not math.isfinite(value):
        return None
    return int(value) if float(value).is_integer() and abs(value) < 2 ** 53 else value


# --------------------------------------------------------------------------- #
# Index cache
# --------------------------------------------------------------------------- #
_MEMORY: "OrderedDict[str, Index]" = OrderedDict()
_LOCK = threading.Lock()
_MEMORY_SLOTS = 6


def index(source: Source) -> Index:
    lim = limits()
    if source.live:
        # A growing file is re-read per poll; it is never cached.
        return Index.parse(source, lim.max_bytes)
    with _LOCK:
        if source.revision in _MEMORY:
            _MEMORY.move_to_end(source.revision)
            return _MEMORY[source.revision]
    root = cache_dir()
    path = root / f"{source.revision}.idx"
    built = Index.load(path, source.revision) if path.is_file() else None
    if built is None:
        built = Index.parse(source, lim.max_bytes)
        try:
            root.mkdir(parents=True, exist_ok=True)
            built.dump(path)
            _evict(root, lim.cache_bytes)
        except OSError:
            pass  # the cache is an accelerator; the parsed index is still exact
    with _LOCK:
        _MEMORY[source.revision] = built
        while len(_MEMORY) > _MEMORY_SLOTS:
            _MEMORY.popitem(last=False)
    return built


def _evict(root: Path, budget: int):
    files = sorted(root.glob("*.idx"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for path in files[:-1]:
        if total <= budget:
            break
        total -= path.stat().st_size
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Original text access (never from the index)
# --------------------------------------------------------------------------- #
def _records(source: Source):
    lim = limits()
    with source.path.open("rb") as stream:
        for number, raw in enumerate(_lines(stream, lim.max_bytes, partial_tail=source.live), 1):
            line = _decode(raw, number)
            yield number, (line.split(",") if '"' not in line else next(csv.reader([line])))


def raw(source: Source, *, offset=0, count=RAW_PAGE):
    if not 0 <= offset <= MAX_ROWS or not 1 <= count <= RAW_PAGE:
        raise CurveError("Invalid raw-data page", "CSV_OPTIONS", 400)
    header, page, more = None, [], False
    for number, cells in _records(source):
        if number == 1:
            header = cells
            continue
        record = number - 1
        if record <= offset:
            continue
        if len(page) == count:
            more = True
            break
        page.append({"row": record, "cells": cells})
    return {"revision": source.revision, "offset": offset, "has_more": more,
            "header": header or [], "rows": page}


def export_csv(source: Source, idx: Index, idents, start=None, end=None, max_lanes=32):
    """Original text of the selected columns for records inside [start, end]."""
    chosen = idx.selection(idents, max_lanes)
    start, end = idx.span(start, end)
    picks = [0] + [c.index for c in chosen]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for number, cells in _records(source):
        if number == 1:
            writer.writerow([cells[i] for i in picks])
        else:
            time = _float(cells[0]) if cells else math.nan
            if math.isnan(time) or time < start:
                continue
            if time > end:
                break
            writer.writerow([cells[i] for i in picks])
        if buffer.tell() > 1 << 16:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()
