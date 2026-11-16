/* Variable-curve waveform renderer (window.LMCurveRender).
 *
 * One drawing routine feeds two painters: a Canvas 2D painter for the live
 * view / PNG export and an SVG painter for vector export, so what is exported
 * is exactly what is on screen. The renderer is pure: it draws a layout model
 * built by task_curves.js and never fetches or mutates state.
 *
 * Lanes are logic-analyzer style: 0/1 signals as a digital line, other
 * discrete signals as a "bus" of value segments labelled with the case's
 * constant names, analog signals as a scaled line (or a per-pixel min/max
 * envelope when the server sent one).
 */
(function (global) {
  "use strict";

  const AXIS_H = 26, BAND_H = 22, PAD_BOTTOM = 6;

  // ---------------------------------------------------------------- painters
  function canvasPainter(ctx) {
    return {
      rect(x, y, w, h, fill) { ctx.fillStyle = fill; ctx.fillRect(x, y, w, h); },
      strokeRect(x, y, w, h, stroke, width) {
        ctx.strokeStyle = stroke; ctx.lineWidth = width || 1; ctx.setLineDash([]);
        ctx.strokeRect(x + .5, y + .5, w - 1, h - 1);
      },
      line(points, stroke, width, dash) {
        if (points.length < 2) return;
        ctx.strokeStyle = stroke; ctx.lineWidth = width || 1; ctx.setLineDash(dash || []);
        ctx.beginPath(); ctx.moveTo(points[0][0], points[0][1]);
        for (let i = 1; i < points.length; i++) ctx.lineTo(points[i][0], points[i][1]);
        ctx.stroke(); ctx.setLineDash([]);
      },
      polygon(points, fill, stroke, width) {
        ctx.beginPath(); ctx.moveTo(points[0][0], points[0][1]);
        for (let i = 1; i < points.length; i++) ctx.lineTo(points[i][0], points[i][1]);
        ctx.closePath();
        if (fill) { ctx.fillStyle = fill; ctx.fill(); }
        if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = width || 1; ctx.setLineDash([]); ctx.stroke(); }
      },
      text(x, y, str, o) {
        ctx.font = (o.weight || "400") + " " + (o.size || 11) + "px " + (o.font || "sans-serif");
        ctx.fillStyle = o.color; ctx.textAlign = o.align || "left"; ctx.textBaseline = o.baseline || "middle";
        ctx.fillText(str, x, y);
      },
      measure(str, o) {
        ctx.font = (o.weight || "400") + " " + (o.size || 11) + "px " + (o.font || "sans-serif");
        return ctx.measureText(str).width;
      },
      clip(x, y, w, h, fn) { ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); fn(); ctx.restore(); },
    };
  }

  function svgPainter(width, height, measureCtx) {
    const out = [];
    let clipId = 0;
    const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const pts = p => p.map(q => q[0].toFixed(1) + "," + q[1].toFixed(1)).join(" ");
    const measure = canvasPainter(measureCtx).measure;
    return {
      rect(x, y, w, h, fill) { out.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${esc(fill)}"/>`); },
      strokeRect(x, y, w, h, stroke, sw) { out.push(`<rect x="${x + .5}" y="${y + .5}" width="${w - 1}" height="${h - 1}" fill="none" stroke="${esc(stroke)}" stroke-width="${sw || 1}"/>`); },
      line(points, stroke, sw, dash) {
        if (points.length < 2) return;
        out.push(`<polyline points="${pts(points)}" fill="none" stroke="${esc(stroke)}" stroke-width="${sw || 1}"${dash ? ` stroke-dasharray="${dash.join(" ")}"` : ""}/>`);
      },
      polygon(points, fill, stroke, sw) {
        out.push(`<polygon points="${pts(points)}" fill="${fill ? esc(fill) : "none"}"${stroke ? ` stroke="${esc(stroke)}" stroke-width="${sw || 1}"` : ""}/>`);
      },
      text(x, y, str, o) {
        const anchor = { left: "start", center: "middle", right: "end" }[o.align || "left"];
        const base = { middle: "central", top: "hanging", bottom: "auto", alphabetic: "auto" }[o.baseline || "middle"];
        out.push(`<text x="${x}" y="${y}" fill="${esc(o.color)}" font-size="${o.size || 11}" font-weight="${o.weight || 400}" font-family="${esc(o.font || "sans-serif")}" text-anchor="${anchor}" dominant-baseline="${base}">${esc(str)}</text>`);
      },
      measure,
      clip(x, y, w, h, fn) {
        const id = "cvclip" + (++clipId);
        out.push(`<clipPath id="${id}"><rect x="${x}" y="${y}" width="${w}" height="${h}"/></clipPath><g clip-path="url(#${id})">`);
        fn(); out.push("</g>");
      },
      done() {
        return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` + out.join("") + "</svg>";
      },
    };
  }

  // ------------------------------------------------------------------- math
  function niceStep(span, target) {
    const raw = span / Math.max(1, target);
    const pow = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / pow;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * pow;
  }
  function decimals(step) { return Math.max(0, Math.min(6, -Math.floor(Math.log10(step) + 1e-9))); }
  function fmtTime(t, step) { return t.toFixed(decimals(step || 0.001)) + " s"; }
  function fmtValue(v) {
    if (v === null || v === undefined) return "—";
    return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(7)));
  }

  function layout(model) {
    let y = AXIS_H + BAND_H;
    model.lanes.forEach(l => { l.y = y; y += l.height; });
    return y + PAD_BOTTOM;
  }

  // Value of a step series at time t (last point at or before t).
  function valueAt(points, t) {
    let lo = 0, hi = points.length - 1, k = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (points[m][0] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
    return k < 0 ? undefined : points[k][1];
  }

  // ----------------------------------------------------------------- render
  function render(p, model) {
    const W = model.width, H = model.height, L = model.labelW, X0 = L, X1 = W - 8;
    const th = model.theme;
    const { start, end } = model.view;
    const span = (end - start) || 1e-9;
    const tx = t => X0 + (t - start) / span * (X1 - X0);
    p.rect(0, 0, W, H, th.surface);

    // Time axis + grid
    const step = niceStep(span, Math.max(2, Math.floor((X1 - X0) / 110)));
    const first = Math.ceil(start / step) * step;
    const ticks = [];
    for (let t = first; t <= end + step * 1e-6 && ticks.length < 200; t += step) ticks.push(t);
    p.rect(0, 0, W, AXIS_H, th.surface2);
    p.line([[0, AXIS_H - .5], [W, AXIS_H - .5]], th.border, 1);
    ticks.forEach(t => {
      const x = Math.round(tx(t)) + .5;
      p.line([[x, AXIS_H - 6], [x, AXIS_H]], th.text3, 1);
      p.line([[x, AXIS_H + BAND_H], [x, H]], th.grid, 1);
      p.text(x, 11, fmtTime(t, step), { color: th.text2, align: "center", size: 11, font: th.font });
    });
    p.text(8, 11, "时间 (s)", { color: th.text3, size: 11, font: th.font });

    // Step band
    const by = AXIS_H;
    p.rect(0, by, W, BAND_H, th.surface);
    p.text(8, by + BAND_H / 2, "判定步骤", { color: th.text3, size: 11, font: th.font });
    p.clip(X0, by, X1 - X0, BAND_H, () => {
      (model.steps || []).forEach(s => {
        if (s.end === null || s.end === undefined) return;
        const a = tx(s.start), b = Math.max(tx(s.end), a + (s.result === "FAIL" ? 4 : 2));
        if (b < X0 || a > X1) return;
        const fill = s.result === "FAIL" ? th.dangerSoft : s.result === "PASS" ? th.okSoft : th.surface2;
        const edge = s.result === "FAIL" ? th.danger : s.result === "PASS" ? th.ok : th.text3;
        p.rect(a, by + 3, b - a, BAND_H - 6, fill);
        p.line([[b, by + 3], [b, by + BAND_H - 3]], edge, s === model.focusStep ? 3 : 1);
        if (b - a > 26) p.text(Math.max(a, X0) + 4, by + BAND_H / 2, "S" + s.step, { color: edge, size: 10, weight: 600, font: th.font });
        if (s === model.focusStep) p.strokeRect(a, by + 2, b - a, BAND_H - 4, th.brand, 2);
      });
    });
    p.line([[0, by + BAND_H - .5], [W, by + BAND_H - .5]], th.border, 1);

    // Lanes
    model.lanes.forEach((lane, i) => {
      const y = lane.y, h = lane.height;
      if (lane.selected) p.rect(0, y, W, h, th.brandSoft);
      else if (i % 2) p.rect(0, y, W, h, th.zebra);
      p.line([[0, y + h - .5], [W, y + h - .5]], th.border, 1);
      // label column
      p.rect(L - 3, y + 6, 3, h - 12, lane.color);
      const name = lane.display;
      const first = y + Math.min(h / 2, 12);
      let room = L - 14;
      if (lane.readout !== undefined) {
        const o = { color: th.brandText, size: 11, weight: 600, align: "right", font: th.mono };
        const w = Math.min(p.measure(lane.readout, o), (L - 20) / 2);
        p.clip(L - 10 - w, y, w + 2, h, () => p.text(L - 10, first, lane.readout, o));
        room = L - 22 - w;
      }
      p.clip(4, y, room, h, () => p.text(8, first, name, { color: th.text, size: 12, weight: 500, font: th.font }));
      if (h >= 34 && lane.subtitle) {
        p.clip(4, y, L - 14, h, () => p.text(8, y + 26, lane.subtitle, { color: th.text3, size: 10, font: th.mono }));
      }
      p.clip(X0, y, X1 - X0, h, () => drawLane(p, lane, tx, X0, X1, th, model));
    });
    p.line([[X0 - .5, AXIS_H], [X0 - .5, H]], th.border, 1);

    // Compare divergence
    if (model.compare && model.compare.first !== null && model.compare.first !== undefined) {
      const x = tx(model.compare.first);
      if (x >= X0 && x <= X1) {
        p.line([[x, AXIS_H], [x, H]], th.danger, 1.5, [6, 4]);
        p.text(x + 4, AXIS_H + BAND_H + 8, "首个分歧", { color: th.danger, size: 10, weight: 600, font: th.font });
      }
    }
    // Cursors
    [["a", th.brand], ["b", th.warn]].forEach(([key, color]) => {
      const t = model.cursors[key];
      if (t === null || t === undefined) return;
      const x = tx(t);
      if (x < X0 || x > X1) return;
      p.line([[x, AXIS_H], [x, H]], color, 1.5);
      p.polygon([[x - 6, 0], [x + 6, 0], [x, 8]], color);
      p.text(x + 8, 4, key.toUpperCase(), { color, size: 10, weight: 700, baseline: "top", font: th.font });
    });
    if (model.hoverX !== null && model.hoverX !== undefined && model.hoverX >= X0 && model.hoverX <= X1) {
      p.line([[model.hoverX, AXIS_H], [model.hoverX, H]], th.text3, 1, [3, 3]);
    }
  }

  function yScale(lane, y, h) {
    const lo = lane.min, hi = lane.max;
    const top = y + 7, bottom = y + h - 7;
    if (lo === null || hi === null || lo === undefined) return () => (top + bottom) / 2;
    if (hi === lo) return () => (top + bottom) / 2;
    return v => bottom - (v - lo) / (hi - lo) * (bottom - top);
  }

  function drawLane(p, lane, tx, X0, X1, th, model) {
    const y = lane.y, h = lane.height;
    const series = lane.series;
    if (!series) { p.text(X0 + 8, y + h / 2, "加载中…", { color: th.text3, size: 11, font: th.font }); return; }
    if (lane.other) drawSeries(p, lane, lane.other, tx, X0, X1, th, true, model);
    drawSeries(p, lane, series, tx, X0, X1, th, false, model);
    if (lane.divergence !== undefined && lane.divergence !== null) {
      const x = tx(lane.divergence);
      p.polygon([[x - 5, y + 1], [x + 5, y + 1], [x, y + 8]], th.danger);
    }
    (lane.checks || []).forEach(c => {
      if (c.at === null || c.at === undefined) return;
      const x = tx(c.at);
      if (x < X0 - 6 || x > X1 + 6) return;
      const cy = y + h - 7;
      if (c.ok) {
        p.polygon([[x, cy - 5], [x + 5, cy], [x, cy + 5], [x - 5, cy]], th.ok, th.surface, 1);
      } else {
        p.polygon([[x, cy - 6], [x + 6, cy + 5], [x - 6, cy + 5]], th.danger, th.surface, 1);
      }
    });
  }

  function drawSeries(p, lane, series, tx, X0, X1, th, ghost, model) {
    const y = lane.y, h = lane.height;
    const color = ghost ? th.ghost : lane.color;
    const width = ghost ? 1.5 : 2;
    const dash = ghost ? [5, 3] : null;
    const endT = model.view.end;
    if (series.mode === "envelope") {
      const ys = yScale(lane, y, h);
      let prev = null;
      series.buckets.forEach(b => {
        const x = tx(b[0]);
        if (b[3] !== null && b[4] !== null) {
          if (prev) p.line([[prev[0], prev[1]], [x, ys(b[1] === null ? b[3] : b[1])]], color, 1, dash);
          p.line([[x, ys(b[3])], [x, ys(b[4])]], color, width, dash);
          prev = [x, ys(b[2] === null ? b[4] : b[2])];
        } else prev = null;
      });
      return;
    }
    const pts = series.points;
    if (!pts.length) return;
    if (lane.kind === "analog") {
      const ys = yScale(lane, y, h);
      let run = [];
      const flush = () => { if (run.length > 1) p.line(run, color, width, dash); else if (run.length === 1) p.rect(run[0][0] - 1, run[0][1] - 1, 2, 2, color); run = []; };
      pts.forEach(q => {
        if (q[1] === null) { flush(); return; }
        run.push([tx(q[0]), ys(q[1])]);
      });
      if (run.length && !ghost) run.push([tx(endT), run[run.length - 1][1]]);
      flush();
      return;
    }
    // Discrete: digital (0/1) line or value bus.
    const segs = [];
    for (let i = 0; i < pts.length; i++) {
      const a = Math.max(X0 - 2, tx(pts[i][0]));
      const b = Math.min(X1 + 2, i + 1 < pts.length ? tx(pts[i + 1][0]) : tx(endT));
      if (b < X0 - 2 || a > X1 + 2) continue;
      segs.push([a, b, pts[i][1]]);
    }
    const top = y + 8, bottom = y + h - 9;
    if (lane.digital) {
      const line = [];
      segs.forEach(([a, b, v]) => {
        const yy = v === null ? (top + bottom) / 2 : v ? top : bottom;
        if (line.length) line.push([a, yy]); else line.push([a, yy]);
        line.push([b, yy]);
        if (v === 1 && !ghost && b - a > 1) p.rect(a, top, b - a, bottom - top, th.fillSoft);
      });
      p.line(line, color, width, dash);
      return;
    }
    const mid = (top + bottom) / 2;
    segs.forEach(([a, b, v]) => {
      if (v === null) { p.line([[a, mid], [b, mid]], th.text3, 1, [2, 2]); return; }
      const k = Math.min(4, (b - a) / 2);
      const poly = [[a, mid], [a + k, top], [b - k, top], [b, mid], [b - k, bottom], [a + k, bottom]];
      if (ghost) { p.polygon(poly, null, color, 1.2); return; }
      p.polygon(poly, th.fillSoft, color, 1.5);
      const label = lane.label ? lane.label(v) : fmtValue(v);
      const avail = b - a - 2 * k - 4;
      if (avail > 10) {
        let text = label;
        if (p.measure(text, { size: 11, font: th.font }) > avail) text = fmtValue(v);
        if (p.measure(text, { size: 11, font: th.font }) <= avail) {
          p.clip(a + k, top, b - a - 2 * k, bottom - top, () =>
            p.text(Math.max(a + k + 3, X0 + 3), mid, text, { color: th.text, size: 11, font: th.font }));
        }
      }
    });
  }

  // ---------------------------------------------------------------- minimap
  function renderMinimap(p, mm) {
    const { width: W, height: H, theme: th, domain, view, labelW: L } = mm;
    const span = (domain[1] - domain[0]) || 1e-9;
    const X0 = L, X1 = W - 8;
    const tx = t => X0 + (t - domain[0]) / span * (X1 - X0);
    p.rect(0, 0, W, H, th.surface2);
    p.text(8, H / 2, "全程", { color: th.text3, size: 11, font: th.font });
    (mm.steps || []).forEach(s => {
      if (s.end === null || s.end === undefined) return;
      const a = tx(s.start), b = Math.max(tx(s.end), a + 1);
      p.rect(a, 3, b - a, 6, s.result === "FAIL" ? th.danger : s.result === "PASS" ? th.okSoftStrong : th.text3);
    });
    // change density: one tick per event bucket
    (mm.density || []).forEach((n, i, arr) => {
      if (!n) return;
      const x = X0 + i / arr.length * (X1 - X0);
      const hh = Math.max(2, Math.min(H - 16, Math.log2(n + 1) * 4));
      p.rect(x, H - 3 - hh, Math.max(1, (X1 - X0) / arr.length - .5), hh, th.densityFill);
    });
    const a = tx(view.start), b = tx(view.end);
    p.rect(X0, 0, Math.max(0, a - X0), H, th.shade);
    p.rect(b, 0, Math.max(0, X1 - b), H, th.shade);
    p.strokeRect(a, 0, Math.max(2, b - a), H, th.brand, 2);
  }

  global.LMCurveRender = { AXIS_H, BAND_H, canvasPainter, svgPainter, render, renderMinimap,
                           layout, valueAt, fmtValue, fmtTime, niceStep };
})(window);
