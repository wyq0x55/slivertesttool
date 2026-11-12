"""Bounded numeric time-series access to a task's fixed output.csv artifact."""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import math
from contextlib import contextmanager

from . import run_evidence_service as evidence

MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_COLUMNS = 256
MAX_POINTS = 2000
MAX_SIGNALS = 8
ENCODINGS = {"utf-8-sig", "utf-8", "utf-16", "gb18030", "cp1252"}
DELIMITERS = {",", ";", "\t", "|"}


class CurveError(ValueError):
    def __init__(self, message, code="CSV_INVALID", status=422):
        super().__init__(message)
        self.code, self.status = code, status


class _LimitedReader(io.RawIOBase):
    """Bound reads even when a file grows after stat()."""
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_BYTES

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.remaining <= 0:
            if self.stream.read(1):
                raise CurveError("CSV exceeds the 64 MiB preview limit", "CSV_TOO_LARGE", 413)
            return 0
        data = self.stream.read(min(len(buffer), self.remaining))
        buffer[:len(data)] = data
        self.remaining -= len(data)
        return len(data)


def artifact(task):
    """Use the task/attempt identity, never a request path or arbitrary report path."""
    try:
        return evidence.result_file(task, "output.csv", max_bytes=MAX_BYTES)
    except (OSError, ValueError) as exc:
        message = str(exc)
        if "size limit" in message:
            raise CurveError("CSV exceeds the 64 MiB preview limit; use the run report download",
                             "CSV_TOO_LARGE", 413) from exc
        raise CurveError("No readable, verified output.csv for this task attempt",
                         "CSV_UNAVAILABLE", 404) from exc


def revision(task, path):
    try:
        info = path.stat()
    except OSError as exc:
        raise CurveError("The task artifact is no longer available; reload", "CSV_CHANGED", 409) from exc
    identity = (task.id, task.run_count or 1, info.st_dev, info.st_ino,
                info.st_size, info.st_mtime_ns)
    return hashlib.sha256(repr(identity).encode()).hexdigest()[:24]


def _number(value, delimiter):
    value = value.strip()
    if delimiter != "," and "," in value and "." not in value:
        value = value.replace(",", ".")
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


class CsvResult:
    def __init__(self, task, *, encoding=None, delimiter=None, expected_revision=None):
        self.task = task
        self.path = artifact(task)
        self.revision = revision(task, self.path)
        if expected_revision and expected_revision != self.revision:
            raise CurveError("The task artifact changed; reload the variables", "CSV_CHANGED", 409)
        with self.path.open("rb") as stream:
            sample = stream.read(65536)
        if not sample.strip(b"\xef\xbb\xbf\xff\xfe\x00\r\n\t "):
            raise CurveError("output.csv is empty", "CSV_EMPTY")
        if encoding and encoding not in ENCODINGS:
            raise CurveError("Unsupported CSV encoding", "CSV_OPTIONS", 400)
        self.encoding = encoding or self._encoding(sample)
        try:
            text = codecs.getincrementaldecoder(self.encoding)(errors="strict").decode(sample, final=False)
        except UnicodeError as exc:
            raise CurveError("Cannot decode CSV with the selected encoding", "CSV_ENCODING") from exc
        # Excel's optional separator directive is explicit, not a header row.
        first = text.splitlines()[0] if text.splitlines() else ""
        self.directive = first.lower().startswith("sep=") and len(first) == 5 and first[-1] in DELIMITERS
        if delimiter and delimiter not in DELIMITERS:
            raise CurveError("Unsupported CSV delimiter", "CSV_OPTIONS", 400)
        if delimiter:
            self.delimiter = delimiter
        elif self.directive:
            self.delimiter = first[-1]
        else:
            try:
                self.delimiter = csv.Sniffer().sniff(text[:16384], delimiters=",;\t|").delimiter
            except csv.Error:
                # A single-column file can still be inspected and downloaded.
                self.delimiter = ","
        with self.rows() as rows:
            try:
                self.header = next(rows)
            except StopIteration as exc:
                raise CurveError("output.csv has no header", "CSV_EMPTY") from exc
        if not 1 <= len(self.header) <= MAX_COLUMNS:
            raise CurveError("CSV must have at most 256 columns", "CSV_TOO_WIDE", 413)
        if any(len(name) > 512 for name in self.header):
            raise CurveError("CSV column names exceed 512 characters")

    @staticmethod
    def _encoding(sample):
        if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16"
        if sample.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        try:
            codecs.getincrementaldecoder("utf-8")(errors="strict").decode(sample, final=False)
            return "utf-8"
        except UnicodeError:
            pass
        candidates = []
        for encoding in ("gb18030", "cp1252"):
            try:
                codecs.getincrementaldecoder(encoding)(errors="strict").decode(sample, final=False)
                candidates.append(encoding)
            except UnicodeError:
                continue
        if len(candidates) == 1:
            return candidates[0]
        raise CurveError("CSV encoding is ambiguous; choose GB18030 or Windows-1252 explicitly",
                         "CSV_ENCODING")

    @contextmanager
    def rows(self):
        before = revision(self.task, self.path)
        if before != self.revision:
            raise CurveError("The task artifact changed; reload", "CSV_CHANGED", 409)
        try:
            with self.path.open("rb") as raw:
                with io.TextIOWrapper(io.BufferedReader(_LimitedReader(raw)), encoding=self.encoding,
                                      errors="strict", newline="") as text:
                    def lines():
                        # csv's own field limit also bounds fields spanning multiple lines.
                        for _ in range(MAX_ROWS * 2 + 2):
                            line = text.readline(131073)
                            if not line:
                                return
                            if len(line) > 131072:
                                raise CurveError("CSV line exceeds 128 Ki characters", "CSV_TOO_LARGE", 413)
                            yield line
                        raise CurveError("CSV exceeds the line limit", "CSV_TOO_LARGE", 413)
                    reader = csv.reader(lines(), delimiter=self.delimiter, strict=True)
                    if self.directive:
                        next(reader, None)
                    yield reader
        except (UnicodeError, csv.Error) as exc:
            raise CurveError("Malformed CSV or encoding mismatch; check the original file",
                             "CSV_INVALID") from exc
        except OSError as exc:
            raise CurveError("CSV is no longer readable; reload the task", "CSV_CHANGED", 409) from exc
        if revision(self.task, self.path) != before:
            raise CurveError("The task artifact changed during reading; reload", "CSV_CHANGED", 409)

    def metadata(self):
        counts = [0] * len(self.header)
        inspected = 0
        with self.rows() as rows:
            next(rows, None)
            for row in rows:
                if len(row) == len(self.header):
                    for i, value in enumerate(row):
                        counts[i] += self.number(value) is not None
                inspected += 1
                if inspected >= 100:
                    break
        names = [name.strip() or f"Column {i + 1}" for i, name in enumerate(self.header)]
        columns = [{"id": f"c{i}", "index": i, "name": names[i],
                    "label": names[i] + (f" · column {i + 1}" if names.count(names[i]) > 1 else ""),
                    "numeric_preview": counts[i] > 0, "unit": None, "type": None}
                   for i in range(len(names))]
        suggested = next((c["id"] for c in columns if c["numeric_preview"] and
                          c["name"].lower() in ("t", "time", "time_s", "time [s]", "time[s]", "timestamp")), None)
        return {"revision": self.revision, "run_count": self.task.run_count or 1,
                "encoding": self.encoding, "delimiter": self.delimiter,
                "columns": columns, "suggested_time": suggested, "inspected_rows": inspected,
                "bytes": self.path.stat().st_size,
                "limits": {"bytes": MAX_BYTES, "rows": MAX_ROWS, "points": MAX_POINTS, "signals": MAX_SIGNALS}}

    def number(self, value):
        return _number(value, self.delimiter)

    def column(self, name):
        if (not isinstance(name, str) or not 2 <= len(name) <= 4
                or not name.startswith("c") or not name[1:].isascii() or not name[1:].isdigit()):
            raise CurveError("Choose a valid CSV column", "CSV_OPTIONS", 400)
        index = int(name[1:])
        if index >= len(self.header):
            raise CurveError("Unknown CSV column", "CSV_OPTIONS", 400)
        return index

    def selection(self, time, columns):
        ti = self.column(time)
        if not 1 <= len(columns) <= MAX_SIGNALS or len(set(columns)) != len(columns):
            raise CurveError("Select 1 to 8 distinct variables", "CSV_OPTIONS", 400)
        indices = [self.column(c) for c in columns]
        if ti in indices:
            raise CurveError("Time cannot also be a variable", "CSV_OPTIONS", 400)
        return ti, indices

    def points(self, ti, indices, stats=None):
        last = None
        with self.rows() as rows:
            next(rows, None)
            for record, row in enumerate(rows, 1):
                if record > MAX_ROWS:
                    raise CurveError("CSV exceeds one million records", "CSV_TOO_LARGE", 413)
                if not row or all(not cell.strip() for cell in row):
                    if stats is not None:
                        stats["blank_rows"] += 1
                    continue
                if len(row) != len(self.header):
                    if stats is not None:
                        stats["malformed_rows"] += 1
                    continue
                time = self.number(row[ti])
                if time is None:
                    if stats is not None:
                        stats["invalid_time_rows"] += 1
                    continue
                if last is not None and time < last:
                    raise CurveError("Time is not monotonic; choose another time column or inspect the raw CSV",
                                     "CSV_TIME_ORDER")
                if stats is not None and time == last:
                    stats["duplicate_time_rows"] += 1
                last = time
                values = [self.number(row[i]) for i in indices]
                if stats is not None:
                    stats["invalid_values"] += sum(v is None for v in values)
                yield {"row": record, "t": time, "values": values}

    def data(self, time, columns, *, start=None, end=None, limit=1600):
        ti, indices = self.selection(time, columns)
        if not 100 <= limit <= MAX_POINTS:
            raise CurveError("Point limit must be between 100 and 2000", "CSV_OPTIONS", 400)
        if start is not None and end is not None and start > end:
            raise CurveError("Range start must not exceed end", "CSV_OPTIONS", 400)
        stats = dict.fromkeys(("blank_rows", "malformed_rows", "invalid_time_rows",
                               "duplicate_time_rows", "invalid_values"), 0)
        total, low, high = 0, None, None
        for point in self.points(ti, indices, stats):
            total += 1
            low = point["t"] if low is None else low
            high = point["t"]
        if not total:
            raise CurveError("CSV has no usable numeric time records", "CSV_NO_DATA")
        lo = low if start is None else max(low, start)
        hi = high if end is None else min(high, end)
        # Shared time buckets retain first/last, per-signal extrema and a null gap.
        bucket_count = max(1, (limit - 2) // (3 * len(indices) + 2))
        buckets, raw_points, count = {}, [], 0
        for point in self.points(ti, indices):
            if not lo <= point["t"] <= hi:
                continue
            count += 1
            if len(raw_points) < limit:
                raw_points.append(point)
            width = hi - lo
            if not math.isfinite(width):
                fraction = (point["t"] / 2 - lo / 2) / (hi / 2 - lo / 2)
            else:
                fraction = (point["t"] - lo) / width if width > 0 else 0
            bucket_id = min(bucket_count - 1, int(fraction * bucket_count))
            bucket = buckets.setdefault(bucket_id, {"first": point, "last": point, "min": {}, "max": {}, "null": {}})
            bucket["last"] = point
            for i, value in enumerate(point["values"]):
                if value is None:
                    bucket["null"].setdefault(i, point)
                else:
                    if i not in bucket["min"] or value < bucket["min"][i]["values"][i]:
                        bucket["min"][i] = point
                    if i not in bucket["max"] or value > bucket["max"][i]["values"][i]:
                        bucket["max"][i] = point
        if count <= limit:
            sampled = raw_points
        else:
            keep = {}
            for b in buckets.values():
                for p in [b["first"], b["last"], *b["min"].values(), *b["max"].values(), *b["null"].values()]:
                    keep[p["row"]] = p
            sampled = sorted(keep.values(), key=lambda p: p["row"])
        return {"revision": self.revision, "columns": columns, "time_column": time,
                "domain": [low, high], "range": [lo, hi], "total_rows": total,
                "range_rows": count, "sampled": count > len(sampled), "points": sampled,
                "sampling": "shared-time-bucket extrema; first/last and null gaps retained",
                "warnings": stats}

    def point(self, time, columns, at):
        ti, indices = self.selection(time, columns)
        best, distance, duplicates = None, math.inf, 0
        for p in self.points(ti, indices):
            diff = abs(p["t"] - at)
            if best is None or diff < distance:
                best, distance, duplicates = p, diff, 1
            elif best and p["t"] == best["t"]:
                duplicates += 1
        if best is None:
            raise CurveError("No usable time records", "CSV_NO_DATA")
        # Return exact CSV text, never an interpolated or sampled value.
        page = self.raw(offset=best["row"] - 1, count=1)
        return {**best, "raw": page["rows"][0]["cells"], "duplicate_times": duplicates,
                "revision": self.revision, "source": "original CSV record; first at duplicate time"}

    def raw(self, *, offset=0, count=100):
        if not 0 <= offset < MAX_ROWS or not 1 <= count <= 100:
            raise CurveError("Invalid raw-data page", "CSV_OPTIONS", 400)
        page, more = [], False
        with self.rows() as rows:
            next(rows, None)
            for record, cells in enumerate(rows, 1):
                if record > MAX_ROWS:
                    raise CurveError("CSV exceeds one million records", "CSV_TOO_LARGE", 413)
                if record <= offset:
                    continue
                if len(page) == count:
                    more = True
                    break
                # Bound even malformed rows containing too many cells.
                if len(cells) > MAX_COLUMNS:
                    raise CurveError("Raw CSV record exceeds 256 columns", "CSV_TOO_WIDE", 413)
                if sum(len(cell) for cell in cells) > 32768:
                    raise CurveError("Raw CSV record exceeds 32 Ki characters", "CSV_TOO_LARGE", 413)
                page.append({"row": record, "cells": cells})
        return {"revision": self.revision, "offset": offset, "has_more": more, "rows": page}
