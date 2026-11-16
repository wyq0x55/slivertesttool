"""Static SVG of a failed case's failure window, for offline reports.

The window is the first failed step (padded); the lanes are the variables that
step checked plus the stimulus inputs the case drives, as recorded in that
attempt's output.csv. Values come from the exact change-point index, so the
picture shows what Silver logged, not a resampled approximation.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from . import curve_context_service as context
from . import task_curves_service as curves

WIDTH, LABEL, LANE, AXIS = 960, 230, 34, 24
MAX_LANES = 8


def _fmt(v):
    if v is None:
        return "—"
    return str(int(v)) if float(v).is_integer() else f"{v:.6g}"


def _ticks(lo, hi, count=8):
    import math
    span = (hi - lo) or 1e-9
    raw = span / count
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    first = math.ceil(lo / step) * step
    out, t = [], first
    while t <= hi + step * 1e-6 and len(out) < 50:
        out.append(t)
        t += step
    return out, step


def failure_svg(task, run_count=None):
    """SVG text for the first failed step, or None when there is nothing to draw."""
    judge = context.timeline(task, run_count)
    first = judge.get("first_failure")
    if not first or first.get("end") is None:
        return None
    try:
        source = curves.resolve(task, run_count)
        idx = curves.index(source)
    except curves.CurveError:
        return None
    step = next(s for s in judge["steps"] if s["step"] == first["step"])
    names = idx.by_name()
    dictionary = context.dictionary(task, run_count, judge=judge)["signals"]
    wanted = [c["var"] for c in step["checks"] if not c["ok"]] + [c["var"] for c in step["checks"]]
    wanted += [n for n, d in dictionary.items() if "input" in d["roles"]]
    lanes = []
    for var in wanted:
        column = names.get(var or "")
        if column is not None and column not in lanes:
            lanes.append(column)
        if len(lanes) == MAX_LANES:
            break
    if not lanes:
        return None
    pad = max((first["end"] - step["start"]) * 0.15, 0.02)
    lo, hi = idx.span(step["start"] - pad, first["end"] + pad)
    data = idx.series([c.id for c in lanes], start=lo, end=hi, width=WIDTH - LABEL,
                      max_lanes=MAX_LANES, point_limit=20000)
    span = (hi - lo) or 1e-9
    x = lambda t: LABEL + (t - lo) / span * (WIDTH - LABEL - 8)  # noqa: E731
    height = AXIS + LANE * len(lanes) + 6
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
           f'viewBox="0 0 {WIDTH} {height}" font-family="sans-serif" font-size="11" role="img" '
           f'aria-label="Step{escape(str(first["step"]))} failure window">',
           f'<rect width="{WIDTH}" height="{height}" fill="#ffffff"/>',
           f'<rect x="{x(step["start"]):.1f}" y="0" width="{max(1.0, x(first["end"]) - x(step["start"])):.1f}" '
           f'height="{height}" fill="#fdecec"/>']
    ticks, unit = _ticks(lo, hi)
    for t in ticks:
        out.append(f'<line x1="{x(t):.1f}" y1="{AXIS - 5}" x2="{x(t):.1f}" y2="{height}" stroke="#eeeeee"/>')
        out.append(f'<text x="{x(t):.1f}" y="13" text-anchor="middle" fill="#555">{escape(_fmt(round(t, 9)))} s</text>')
    out.append(f'<text x="6" y="13" fill="#888">Step{escape(str(first["step"]))} FAIL</text>')
    checks = {}
    for check in step["checks"]:
        checks.setdefault(check["var"], []).append(check)
    for i, (column, entry) in enumerate(zip(lanes, data["series"])):
        top = AXIS + i * LANE
        mid = top + LANE / 2
        title = (dictionary.get(column.name) or {}).get("name") or column.name
        out.append(f'<line x1="0" y1="{top + LANE - .5}" x2="{WIDTH}" y2="{top + LANE - .5}" stroke="#e5e5e5"/>')
        out.append(f'<text x="6" y="{mid + 4:.1f}" fill="#222">{escape(title[:34])}</text>')
        labels = (dictionary.get(column.name) or {}).get("labels") or {}
        points = entry.get("points") or [[b[0], b[2]] for b in entry.get("buckets", [])]
        lane_lo, lane_hi = column.low, column.high
        scale = (lane_hi - lane_lo) or 1
        poly = []
        for k, (t, v, *_rest) in enumerate(points):
            t_end = points[k + 1][0] if k + 1 < len(points) else hi
            if v is None:
                continue
            y = top + LANE - 5 - (v - lane_lo) / scale * (LANE - 19)
            poly += [f"{x(t):.1f},{y:.1f}", f"{x(t_end):.1f},{y:.1f}"]
            if column.kind() != "analog" and x(t_end) - x(t) > 46:
                name = sorted(labels.get(_fmt(v), []), key=len)
                text = _fmt(v) + (" " + name[0] if name else "")
                out.append(f'<text x="{x(t) + 3:.1f}" y="{top + 11}" fill="#555" font-size="10">{escape(text[:28])}</text>')
        if poly:
            out.append(f'<polyline points="{" ".join(poly)}" fill="none" stroke="#5e6ad2" stroke-width="1.6"/>')
        for check in checks.get(column.name, []):
            if check["at"] is None:
                continue
            cx = x(check["at"])
            color = "#12a150" if check["ok"] else "#e5484d"
            out.append(f'<circle cx="{cx:.1f}" cy="{top + LANE - 6}" r="4" fill="{color}"/>')
    out.append("</svg>")
    return "".join(out)
