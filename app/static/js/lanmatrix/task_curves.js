/* Variable curves for a task attempt (CsvWriter output.csv).
 *
 * The server owns parsing and exactness: it returns change points per signal
 * (or a per-pixel min/max envelope when a signal has more changes than the
 * configured limit in the requested range), the judge timeline and the signal
 * dictionary. This controller owns view state only: lanes, time window,
 * cursors, comparison, saved views and the URL. Drawing is LMCurveRender.
 */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const panel = $("lm-curves-content");
  if (!panel || !window.LMCurveRender) return;
  const R = window.LMCurveRender;
  const canvas = $("cv-canvas"), mini = $("cv-minimap"), wrap = $("cv-wrap"), tip = $("cv-tip");
  const LABEL_W = 240, MINI_H = 44;
  const HEIGHT = { digital: 30, discrete: 36, analog: 92, constant: 30 };

  // ------------------------------------------------------------------ state
  let task = null;            // {projectId, taskKey}
  let ctx = null;             // context response for the open attempt
  let gen = 0;                // bumps on every task/attempt switch
  let lanes = [];             // [{id, name, height}]
  let view = null;            // {start, end}
  let cursors = { a: null, b: null };
  let focusLane = -1, focusStep = null, hoverX = null;
  let filter = "judged";
  let loaded = null;          // {start, end, span, key} of the fetched series window
  let series = new Map();     // column id -> series entry
  let other = new Map();      // column id -> series entry of the comparison
  let compare = null;         // {taskKey, run, result, byName}
  let readouts = new Map();   // column id -> value at cursor A
  let views = [], currentView = null;
  let liveTimer = 0, fetchTimer = 0, urlTimer = 0;
  let pending = new Set();
  let eventsOffset = 0, rawOffset = 0, lower = "events";
  let colorSlots = new Map();
  let defaultKey = "";

  // ---------------------------------------------------------------- helpers
  const css = name => getComputedStyle(panel).getPropertyValue(name).trim();
  function theme() {
    return {
      surface: css("--surface"), surface2: css("--surface-2"), border: css("--border"),
      grid: css("--cv-grid"), zebra: css("--cv-zebra"), text: css("--text"), text2: css("--text-2"),
      text3: css("--text-3"), brand: css("--brand"), brandSoft: css("--brand-soft"),
      brandText: css("--brand-text"), ok: css("--ok"), okSoft: css("--ok-soft"),
      okSoftStrong: css("--cv-ok-strong"), danger: css("--danger"), dangerSoft: css("--danger-soft"),
      warn: css("--warn"), ghost: css("--cv-ghost"), fillSoft: css("--cv-fill"),
      shade: css("--cv-shade"), densityFill: css("--cv-density"),
      font: css("--font") || "sans-serif", mono: css("--mono") || "monospace",
    };
  }
  const status = text => { $("cv-status").textContent = text; };
  const esc = s => String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const columnById = id => ctx && ctx.columns.find(c => c.id === id);
  const columnByName = name => ctx && ctx.columns.find(c => c.name === name);
  const dict = name => (ctx && ctx.dictionary.signals[name]) || null;
  const display = c => (dict(c.name) && dict(c.name).name) || c.name;
  function valueLabel(c, v) {
    if (v === null || v === undefined) return "缺失";
    const d = dict(c.name);
    const names = d && d.labels[String(v)];
    if (!names || !names.length) return R.fmtValue(v);
    const short = names.toSorted((x, y) => x.length - y.length)[0];
    return R.fmtValue(v) + " " + short;
  }
  function kindOf(c) {
    if (c.kind === "discrete" && c.values && c.values.every(v => v === 0 || v === 1)) return "digital";
    return c.kind;
  }
  function laneHeight(c) { return HEIGHT[kindOf(c)] || 36; }
  function colorFor(id) {
    if (!colorSlots.has(id)) {
      const used = new Set(colorSlots.values());
      let slot = 0; while (used.has(slot) && slot < 8) slot++;
      colorSlots.set(id, slot % 8);
    }
    return css("--curve-series-" + (colorSlots.get(id) + 1));
  }
  function stepAt(t) {
    if (!ctx || !ctx.judge.steps) return null;
    // A step owns (start, end]: its verdict instant belongs to it. When
    // several steps end at the same instant (a zero-length step after its
    // predecessor), the last of them is the one that judged at that time.
    const steps = ctx.judge.steps.filter(s => s.end !== null);
    const ending = steps.filter(s => s.end === t);
    if (ending.length) return ending[ending.length - 1];
    return steps.find(s => t > s.start && t < s.end) || (t === 0 && steps[0]) || null;
  }
  function groupOf(name) {
    const dot = name.search(/[.[]/);
    if (dot > 0) {
      const head = name.slice(0, dot);
      const can = head.match(/^(SILS_)?CanIf_\w*?_?(0x[0-9A-Fa-f]+h?)$/);
      return can ? "CanIf " + can[2] : head;
    }
    const m = name.match(/^(.*?)_(?:PP|SR|CS|RP)_/);
    if (m) return m[1];
    const parts = name.split("_");
    return parts.length > 2 ? parts.slice(0, 2).join("_") : "其他";
  }
  function params(extra) {
    const q = Object.assign({}, extra || {});
    if (ctx) {
      q.run = ctx.task.run;
      if (ctx.live) q.live = "1"; else q.revision = ctx.revision;
    }
    return q;
  }
  async function api(part, query, key) {
    const epoch = gen, controller = new AbortController();
    pending.add(controller);
    try {
      const data = await LMApi.taskCurves(task.projectId, key || task.taskKey, part, query, controller.signal);
      return epoch === gen ? data : null;
    } catch (error) {
      if (epoch !== gen || error.name === "AbortError") return null;
      throw error;
    } finally { pending.delete(controller); }
  }
  function abortAll() { pending.forEach(c => c.abort()); pending.clear(); }

  // -------------------------------------------------------------- lifecycle
  function reset() {
    gen++; abortAll();
    clearInterval(liveTimer); liveTimer = 0;
    ctx = null; lanes = []; view = null; cursors = { a: null, b: null };
    focusLane = -1; focusStep = null; hoverX = null; loaded = null;
    series = new Map(); other = new Map(); compare = null; readouts = new Map();
    views = []; currentView = null; colorSlots = new Map(); eventsOffset = 0; rawOffset = 0; defaultKey = "";
    $("cv-body").hidden = true; $("cv-lower").hidden = true; $("cv-live").hidden = true;
    $("cv-first-fail").hidden = true; $("cv-compare-note").hidden = true;
    $("cv-run").replaceChildren(); $("cv-compare").replaceChildren(new Option("不对比", ""));
    $("cv-view").replaceChildren(new Option("默认（期望优先）", ""));
    $("cv-tree").replaceChildren(); tip.hidden = true;
    ["cv-cur-a", "cv-cur-b", "cv-cur-d"].forEach(id => { $(id).textContent = "—"; });
    $("cv-cur-step").textContent = "";
    ["cv-clear-a", "cv-clear-b"].forEach(id => { $(id).hidden = true; });
  }

  async function load(run, restore) {
    if (!task) return;
    reset();
    status("正在读取并索引 output.csv…");
    const epoch = gen;
    try {
      const query = {};
      if (run) query.run = run;
      const data = await api("", query);
      if (!data || epoch !== gen) return;
      ctx = data;
    } catch (error) {
      if (epoch === gen) status(error.code === "CSV_PENDING" ? "任务运行中，CsvWriter 尚未写出 output.csv；稍后自动重试。" : error.message);
      if (epoch === gen && error.code === "CSV_PENDING") liveTimer = setTimeout(() => load(run, restore), 3000);
      return;
    }
    view = { start: ctx.domain[0], end: ctx.domain[1] };
    fillRuns(); fillCompare(); summary();
    $("cv-body").hidden = false; $("cv-lower").hidden = false;
    $("cv-live").hidden = !ctx.live;
    $("cv-download").hidden = !ctx.can_download;
    $("cv-download").href = LMApi.taskCurvesUrl(task.projectId, task.taskKey, "/download", params());
    $("cv-first-fail").hidden = !ctx.judge.first_failure;
    const judged = judgedNames();
    filter = judged.size ? "judged" : "changed";
    syncFilterChips();
    await loadViews();
    if (epoch !== gen) return;
    if (restore && restore.lanes) applyLayout({ lanes: restore.lanes.map(name => ({ name })), range: restore.range });
    else if (currentView) applyLayout(currentView.layout);
    else defaultLanes();
    if (restore && restore.compare) { $("cv-compare").value = restore.compare; await setCompare(restore.compare); }
    tree();
    if (ctx.live) liveTimer = setInterval(poll, 2000);
    if (restore && restore.a !== null) cursors.a = restore.a;
    if (restore && restore.b !== null) cursors.b = restore.b;
    showCursors(); refreshCursor();
    requestDraw(true);
    loadLower();
  }

  function judgedNames() {
    const names = new Set();
    if (!ctx) return names;
    Object.keys(ctx.dictionary.signals).forEach(n => { if (columnByName(n)) names.add(n); });
    return names;
  }

  function defaultLanes() {
    const judged = judgedNames();
    const role = n => (dict(n) && dict(n).roles) || [];
    let picks = ctx.columns.filter(c => judged.has(c.name));
    // Expected/check signals first, then stimulus inputs, so the default view
    // is not filled by inputs before the values the case actually judges.
    const rank = n => role(n).includes("check") ? 0 : role(n).includes("input") ? 1 : 2;
    picks.sort((a, b) => rank(a.name) - rank(b.name));
    if (!picks.length) picks = ctx.columns.filter(c => c.kind !== "constant").slice(0, 8);
    lanes = picks.slice(0, ctx.limits.lanes).map(c => ({ id: c.id, name: c.name, height: laneHeight(c) }));
    defaultKey = lanes.map(l => l.name).join(",");
  }

  function applyLayout(layout) {
    lanes = [];
    (layout.lanes || []).forEach(l => {
      const c = columnByName(l.name);
      if (c && !lanes.some(x => x.id === c.id) && lanes.length < ctx.limits.lanes) {
        lanes.push({ id: c.id, name: c.name, height: l.height || laneHeight(c) });
      }
    });
    if (Array.isArray(layout.range) && layout.range.length === 2) {
      view = clampView(layout.range[0], layout.range[1]);
    }
  }

  function fillRuns() {
    const sel = $("cv-run");
    sel.replaceChildren();
    ctx.attempts.toReversed().forEach(n => {
      sel.add(new Option("第 " + n + " 次" + (n === ctx.task.current_run ? "（最新）" : ""), String(n)));
    });
    if (!ctx.attempts.includes(ctx.task.run)) sel.add(new Option("第 " + ctx.task.run + " 次（运行中）", String(ctx.task.run)));
    sel.value = String(ctx.task.run);
    sel.disabled = sel.options.length < 2;
  }

  async function fillCompare() {
    const sel = $("cv-compare");
    sel.replaceChildren(new Option("不对比", ""));
    const runs = document.createElement("optgroup"); runs.label = "本任务的其他执行";
    ctx.attempts.filter(n => n !== ctx.task.run).toReversed()
      .forEach(n => runs.append(new Option("第 " + n + " 次执行", task.taskKey + ":" + n)));
    if (runs.children.length) sel.append(runs);
    const epoch = gen;
    try {
      const data = await LMApi.listProjectTasks(task.projectId, 200);
      if (epoch !== gen) return;
      const group = document.createElement("optgroup"); group.label = "项目内其他任务（最新执行）";
      (data.tasks || []).filter(t => t.task_id !== task.taskKey && t.has_result)
        .forEach(t => group.append(new Option(t.test_id + " · " + t.task_id + (t.result ? " · " + t.result : ""), t.task_id + ":")));
      if (group.children.length) sel.append(group);
      const wanted = window.LMUrl && LMUrl.get("ccmp");
      if (wanted && !sel.value) sel.value = wanted;
    } catch (_e) { /* other tasks stay unavailable for comparison */ }
  }

  function summary() {
    const j = ctx.judge;
    const parts = [ctx.rows.toLocaleString() + " 条记录",
      (ctx.sample_interval ? R.fmtValue(ctx.sample_interval * 1000) + " ms 采样" : ""),
      R.fmtValue(ctx.domain[0]) + "–" + R.fmtValue(ctx.domain[1]) + " s",
      ctx.columns.length + " 个变量（" + ctx.columns.filter(c => c.kind === "constant").length + " 个恒定）"];
    if (j.available) parts.push("判定 " + (j.verdict || "—") + (j.first_failure ? " · 首个失败 Step" + j.first_failure.step + " @" + R.fmtValue(j.first_failure.at) + " s" : ""));
    else parts.push("无判定日志");
    if (ctx.dictionary.source === "judge" && j.available) parts.push("变量名来自判定日志");
    status(parts.filter(Boolean).join(" · "));
  }

  async function poll() {
    if (!ctx || !ctx.live) return;
    const epoch = gen;
    try {
      const data = await api("", { live: "1" });
      if (!data || epoch !== gen) return;
      const following = view && Math.abs(view.end - ctx.domain[1]) < 1e-9;
      const keepLanes = lanes, keepCursor = cursors;
      ctx = data;
      lanes = keepLanes.filter(l => columnByName(l.name)).map(l => Object.assign(l, { id: columnByName(l.name).id }));
      cursors = keepCursor;
      if (following) { const span = view.end - view.start; view = clampView(ctx.domain[1] - span, ctx.domain[1]); }
      summary(); tree();
      if (!ctx.live) { clearInterval(liveTimer); liveTimer = 0; load(); return; }
      loaded = null; requestDraw(true);
    } catch (error) {
      if (error.code === "CSV_OPTIONS") { clearInterval(liveTimer); liveTimer = 0; load(); }
    }
  }

  // -------------------------------------------------------------- the tree
  function tree() {
    if (!ctx) return;
    const q = $("cv-search").value.trim().toLowerCase();
    const judged = judgedNames();
    const shown = new Set(lanes.map(l => l.id));
    const match = c => {
      if (q && !(c.name.toLowerCase().includes(q) || display(c).toLowerCase().includes(q))) return false;
      if (filter === "judged") return judged.has(c.name);
      if (filter === "changed") return c.kind !== "constant";
      return true;
    };
    const groups = new Map();
    ctx.columns.filter(match).forEach(c => {
      const g = groupOf(c.name);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(c);
    });
    const host = $("cv-tree");
    host.replaceChildren();
    groups.forEach((cols, name) => {
      const box = document.createElement("details");
      box.className = "cv-gr"; box.open = groups.size <= 6 || cols.some(c => shown.has(c.id)) || Boolean(q);
      const sum = document.createElement("summary");
      const all = document.createElement("input");
      all.type = "checkbox";
      all.className = "cv-gr-all";
      all.title = "选择或取消该分类下的全部变量";
      all.setAttribute("aria-label", "选择或取消分类 " + name);
      const picked = cols.filter(c => shown.has(c.id)).length;
      all.checked = cols.length > 0 && picked === cols.length;
      all.indeterminate = picked > 0 && picked < cols.length;
      const halt = ev => ev.stopPropagation();
      all.addEventListener("mousedown", halt);
      all.addEventListener("click", halt);
      all.addEventListener("change", () => setGroup(cols, all.checked));
      const title = document.createElement("span");
      title.className = "cv-gr-name";
      title.textContent = name;
      const count = document.createElement("span");
      count.className = "submuted";
      count.textContent = String(cols.length);
      sum.append(all, title, count);
      box.append(sum);
      const active = cols.filter(c => c.kind !== "constant"), constant = cols.filter(c => c.kind === "constant");
      active.forEach(c => box.append(item(c, shown, judged)));
      if (constant.length) {
        const sub = document.createElement("details"); sub.className = "cv-const";
        sub.innerHTML = `<summary><span>恒定（值不变）</span><span class="submuted">${constant.length}</span></summary>`;
        constant.forEach(c => sub.append(item(c, shown, judged)));
        box.append(sub);
      }
      host.append(box);
    });
    if (!groups.size) host.innerHTML = '<p class="submuted cv-none">没有匹配的变量</p>';
    $("cv-count").textContent = "泳道 " + lanes.length + " / " + ctx.limits.lanes + " · 共 " + ctx.columns.length + " 个变量";
  }

  function item(c, shown, judged) {
    const row = document.createElement("label");
    row.className = "cv-item" + (shown.has(c.id) ? " on" : "");
    const input = document.createElement("input"); input.type = "checkbox"; input.checked = shown.has(c.id);
    input.addEventListener("change", () => toggleLane(c, input.checked));
    const sw = document.createElement("span"); sw.className = "cv-sw";
    if (shown.has(c.id)) sw.style.background = colorFor(c.id);
    const text = document.createElement("span"); text.className = "cv-item-text";
    const title = document.createElement("span"); title.className = "cv-item-name"; title.textContent = display(c);
    text.append(title);
    if (display(c) !== c.name) { const sub = document.createElement("code"); sub.textContent = c.name; sub.title = c.name; text.append(sub); }
    const meta = document.createElement("span"); meta.className = "cv-item-meta";
    const roles = (dict(c.name) && dict(c.name).roles) || [];
    const badges = [];
    if (roles.includes("input")) badges.push('<i class="cv-b in">输入</i>');
    if (roles.includes("check")) badges.push('<i class="cv-b ck">检查</i>');
    const kind = kindOf(c);
    badges.push(kind === "constant" ? `<i class="cv-b">= ${esc(valueLabel(c, c.initial))}</i>`
      : `<i class="cv-b">${{ digital: "开关", discrete: "离散", analog: "连续" }[kind]} · ${c.changes} 次跳变</i>`);
    meta.innerHTML = badges.join("");
    text.append(meta);
    row.append(input, sw, text);
    return row;
  }

  function toggleLane(c, on) {
    if (on) {
      if (lanes.length >= ctx.limits.lanes) { status("最多同时显示 " + ctx.limits.lanes + " 条泳道（可在系统设置中调整）。"); tree(); return; }
      lanes.push({ id: c.id, name: c.name, height: laneHeight(c) });
    } else {
      lanes = lanes.filter(l => l.id !== c.id);
      colorSlots.delete(c.id);
    }
    markViewDirty(); tree(); requestDraw(true); refreshCursor();
  }

  function setGroup(cols, on) {
    const focusId = focusLane >= 0 && lanes[focusLane] ? lanes[focusLane].id : null;
    if (on) {
      const ordered = cols.filter(c => c.kind !== "constant").concat(cols.filter(c => c.kind === "constant"));
      let capped = false;
      ordered.forEach(c => {
        if (capped || lanes.some(l => l.id === c.id)) return;
        if (lanes.length >= ctx.limits.lanes) { capped = true; return; }
        lanes.push({ id: c.id, name: c.name, height: laneHeight(c) });
      });
      if (capped) status("最多同时显示 " + ctx.limits.lanes + " 条泳道（可在系统设置中调整）。");
    } else {
      const drop = new Set(cols.map(c => c.id));
      lanes.forEach(l => { if (drop.has(l.id)) colorSlots.delete(l.id); });
      lanes = lanes.filter(l => !drop.has(l.id));
    }
    focusLane = focusId === null ? -1 : lanes.findIndex(l => l.id === focusId);
    markViewDirty(); tree(); requestDraw(true); refreshCursor();
  }

  function syncFilterChips() {
    document.querySelectorAll(".cv-chip").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.filter === filter)));
  }

  // ------------------------------------------------------------- the view
  function clampView(start, end) {
    const [lo, hi] = ctx.domain;
    const minSpan = Math.max((ctx.sample_interval || 1e-3) * 4, 1e-6);
    let span = Math.max(minSpan, Math.min(end - start, hi - lo || minSpan));
    start = Math.max(lo, Math.min(start, hi - span));
    return { start, end: Math.min(hi, start + span) || start + span };
  }
  function plotWidth() { return Math.max(100, wrap.clientWidth - LABEL_W - 8); }
  function timeAtX(x) { return view.start + (x - LABEL_W) / plotWidth() * (view.end - view.start); }
  function xAtTime(t) { return LABEL_W + (t - view.start) / (view.end - view.start) * plotWidth(); }

  let drawQueued = false;
  function requestDraw(refetch) {
    if (refetch) scheduleFetch();
    if (drawQueued) return;
    drawQueued = true;
    requestAnimationFrame(() => { drawQueued = false; draw(); });
  }

  function scheduleFetch() {
    clearTimeout(fetchTimer);
    fetchTimer = setTimeout(fetchSeries, 60);
  }

  function needsFetch() {
    if (!loaded) return true;
    const ids = lanes.map(l => l.id).join(",");
    if (ids !== loaded.ids || (compare && compare.key !== loaded.compare)) return true;
    if (view.start < loaded.start || view.end > loaded.end) return true;
    const enveloped = [...series.values(), ...other.values()].some(s => s.mode === "envelope");
    const ratio = (view.end - view.start) / loaded.span;
    return enveloped && (ratio < 0.6 || ratio > 1.6);
  }

  async function fetchSeries() {
    if (!ctx || panel.hidden || !lanes.length || !needsFetch()) return;
    const span = view.end - view.start;
    const start = Math.max(ctx.domain[0], view.start - span), end = Math.min(ctx.domain[1], view.end + span);
    const width = Math.min(8192, Math.round(plotWidth() * (end - start) / span));
    const ids = lanes.map(l => l.id);
    const epoch = gen;
    try {
      const data = await api("/series", params({ columns: ids.join(","), start, end, width: Math.max(50, width) }));
      if (!data || epoch !== gen) return;
      series = new Map(data.series.map(s => [s.id, s]));
      other = new Map();
      if (compare) {
        const pairs = ids.map(id => [id, compare.byName.get(columnById(id).name)]).filter(p => p[1]);
        if (pairs.length) {
          const q = { columns: pairs.map(p => p[1]).join(","), start, end, width: Math.max(50, width) };
          if (compare.run) q.run = compare.run;
          const theirs = await api("/series", q, compare.taskKey);
          if (!theirs || epoch !== gen) return;
          const byOther = new Map(theirs.series.map(s => [s.id, s]));
          pairs.forEach(([mine, theirsId]) => other.set(mine, byOther.get(theirsId)));
        }
      }
      loaded = { start, end, span, ids: ids.join(","), compare: compare && compare.key };
      draw();
    } catch (error) { if (epoch === gen) status(error.message); }
  }

  function model(width) {
    const th = theme();
    const checksByVar = new Map();
    (ctx.judge.steps || []).forEach(s => s.checks.forEach(c => {
      if (!c.var) return;
      if (!checksByVar.has(c.var)) checksByVar.set(c.var, []);
      checksByVar.get(c.var).push(c);
    }));
    const divergence = new Map(((compare && compare.result && compare.result.signals) || []).map(s => [s.id, s.t]));
    const out = lanes.map((l, i) => {
      const c = columnById(l.id);
      const kind = kindOf(c);
      const mine = series.get(l.id);
      const theirs = other.get(l.id);
      let [min, max] = kind === "analog" ? visibleRange(mine, theirs) : [c.min, c.max];
      if (min === null) { min = c.min; max = c.max; }
      if (kind !== "analog" && theirs && theirs.points) theirs.points.forEach(p => { if (p[1] !== null) { min = Math.min(min, p[1]); max = Math.max(max, p[1]); } });
      return {
        id: l.id, display: display(c), subtitle: display(c) !== c.name ? c.name : "",
        kind: kind === "digital" || kind === "constant" ? "discrete" : kind,
        height: l.height, color: colorFor(l.id),
        series: mine, other: theirs, min, max, selected: i === focusLane,
        label: v => valueLabel(c, v), checks: checksByVar.get(c.name) || [],
        divergence: divergence.get(l.id),
        readout: cursors.a !== null && readouts.has(l.id) ? valueLabel(c, readouts.get(l.id)) : undefined,
      };
    });
    const m = { width, labelW: LABEL_W, theme: th, view, lanes: out, steps: ctx.judge.steps,
                cursors, hoverX, focusStep, compare: compare && compare.result };
    m.height = R.layout(m);
    return m;
  }

  // Analog lanes scale to what is on screen (value holding at the left edge
  // included), so a short excursion is not flattened by the global range.
  function visibleRange(...entries) {
    let lo = Infinity, hi = -Infinity;
    const take = v => { if (v !== null && v !== undefined) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
    entries.forEach(e => {
      if (!e) return;
      if (e.mode === "envelope") {
        e.buckets.forEach(b => { if (b[0] >= view.start && b[0] <= view.end) { take(b[3]); take(b[4]); } });
        return;
      }
      const pts = e.points;
      pts.forEach((p, i) => {
        const next = i + 1 < pts.length ? pts[i + 1][0] : Infinity;
        if (next >= view.start && p[0] <= view.end) take(p[1]);
      });
    });
    return lo === Infinity ? [null, null] : [lo, hi];
  }

  function draw() {
    if (!ctx || panel.hidden) return;
    $("cv-empty").hidden = lanes.length > 0;
    const width = wrap.clientWidth;
    const m = model(width);
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width = width + "px"; canvas.style.height = m.height + "px";
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(m.height * dpr);
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    R.render(R.canvasPainter(g), m);
    drawMinimap(m.theme, width);
    lastModel = m;
  }
  let lastModel = null;

  let density = null;
  async function loadDensity() {
    density = null;
    if (!ctx) return;
    const epoch = gen;
    try {
      const data = await api("/events", params({ limit: 500 }));
      if (!data || epoch !== gen) return;
      const buckets = new Array(200).fill(0), span = (ctx.domain[1] - ctx.domain[0]) || 1;
      data.events.forEach(e => { buckets[Math.min(199, Math.floor((e.t - ctx.domain[0]) / span * 200))]++; });
      density = buckets; drawMinimap(theme(), wrap.clientWidth);
    } catch (_e) { /* the minimap still shows steps and the window */ }
  }

  function drawMinimap(th, width) {
    const dpr = window.devicePixelRatio || 1;
    mini.style.width = width + "px"; mini.style.height = MINI_H + "px";
    mini.width = Math.round(width * dpr); mini.height = Math.round(MINI_H * dpr);
    const g = mini.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
    R.renderMinimap(R.canvasPainter(g), { width, height: MINI_H, theme: th, domain: ctx.domain, view,
                                          labelW: LABEL_W, steps: ctx.judge.steps, density });
  }

  function setView(start, end) {
    view = clampView(start, end);
    markUrl(); requestDraw(true);
  }

  // -------------------------------------------------------------- cursors
  async function setCursor(key, t) {
    cursors[key] = t === null ? null : Math.max(ctx.domain[0], Math.min(ctx.domain[1], t));
    showCursors();
    if (key === "a") await refreshCursor();
    requestDraw(false); markUrl();
  }
  function showCursors() {
    const fmt = t => t === null ? "—" : R.fmtValue(Number(t.toFixed(6))) + " s";
    $("cv-cur-a").textContent = fmt(cursors.a);
    $("cv-cur-b").textContent = fmt(cursors.b);
    $("cv-cur-d").textContent = cursors.a !== null && cursors.b !== null ? fmt(Math.abs(cursors.b - cursors.a)) : "—";
    $("cv-clear-a").hidden = cursors.a === null;
    $("cv-clear-b").hidden = cursors.b === null;
    const s = cursors.a !== null ? stepAt(cursors.a) : null;
    $("cv-cur-step").textContent = s ? "A 在 Step" + s.step + "（" + (s.result || "—") + "）" + s.title : "";
  }
  async function refreshCursor() {
    readouts = new Map();
    if (!ctx || cursors.a === null || !lanes.length) { requestDraw(false); return; }
    const epoch = gen;
    try {
      const data = await api("/values", params({ columns: lanes.map(l => l.id).join(","), at: cursors.a }));
      if (!data || epoch !== gen) return;
      Object.entries(data.values).forEach(([id, v]) => readouts.set(id, v));
      requestDraw(false);
    } catch (error) { status(error.message); }
  }
  async function jumpEdge(key, dir) {
    if (focusLane < 0 || !lanes[focusLane]) { status("先点击左侧泳道名选中一条泳道。"); return; }
    const from = cursors[key] !== null ? cursors[key] : (dir > 0 ? view.start : view.end);
    try {
      const data = await api("/edge", params({ column: lanes[focusLane].id, at: from, dir }));
      if (!data) return;
      if (data.t === null) { status("该方向没有更多跳变。"); return; }
      await setCursor(key, data.t);
      ensureVisible(data.t);
    } catch (error) { status(error.message); }
  }
  function ensureVisible(t) {
    if (t >= view.start && t <= view.end) return;
    const span = view.end - view.start;
    setView(t - span / 2, t + span / 2);
  }

  // ------------------------------------------------------------ judge link
  function focusOnStep(step) {
    if (!ctx) return;
    const s = ctx.judge.steps.find(x => String(x.step) === String(step));
    if (!s || s.end === null) { status("该步骤没有判定时刻。"); return; }
    focusStep = s;
    const pad = Math.max((s.end - s.start) * 0.15, (ctx.sample_interval || 0.001) * 20);
    setView(s.start - pad, s.end + pad);
    const bad = s.checks.find(c => !c.ok) || s.checks[0];
    ensureLanes(s.checks.map(c => c.var));
    setCursor("a", bad && bad.at !== null ? bad.at : s.end);
  }
  function ensureLanes(vars) {
    let added = false;
    vars.forEach(v => {
      const c = v && columnByName(v);
      if (c && !lanes.some(l => l.id === c.id) && lanes.length < ctx.limits.lanes) {
        lanes.push({ id: c.id, name: c.name, height: laneHeight(c) }); added = true;
      }
    });
    if (added) { tree(); markViewDirty(); requestDraw(true); }
  }
  function firstFailure() {
    const f = ctx && ctx.judge.first_failure;
    if (!f) return;
    focusOnStep(f.step);
    if (f.var) {
      const i = lanes.findIndex(l => l.name === f.var);
      if (i >= 0) focusLane = i;
    }
  }

  // ------------------------------------------------------------ comparison
  async function setCompare(value) {
    compare = null; other = new Map(); loaded = null;
    $("cv-compare-note").hidden = true;
    if (!value) { requestDraw(true); markUrl(); return; }
    const [key, run] = value.split(":");
    const query = params({ other: key });
    if (run) query.other_run = run;
    try {
      const data = await api("/compare", query);
      if (!data) return;
      compare = { key: value, taskKey: key, run: run ? Number(run) : null, result: data,
                  byName: new Map(data.other.columns.map(c => [c.name, c.id])) };
      const first = data.first;
      const note = $("cv-compare-note");
      note.hidden = false;
      note.textContent = (first ? "首个分歧：" + display(columnById(first.id)) + " @" + R.fmtValue(first.t) + " s。"
        : "共同变量全程一致。") + " 虚线为 " + data.other.test_id + " 第 " + data.other.run + " 次执行"
        + (data.only_here.length || data.only_there.length ? "；仅本次有 " + data.only_here.length + " 个、仅对比方有 " + data.only_there.length + " 个变量" : "") + "。";
      if (first) { ensureLanes([columnById(first.id).name]); }
      requestDraw(true); markUrl();
    } catch (error) { status(error.message); $("cv-compare").value = ""; }
  }

  // ---------------------------------------------------------- saved views
  async function loadViews() {
    try {
      const data = await LMApi.curveViews(task.projectId, ctx.task.test_id);
      views = data.views || [];
    } catch (_e) { views = []; }
    const sel = $("cv-view");
    sel.replaceChildren(new Option("默认（期望优先）", ""));
    views.forEach(v => sel.add(new Option(v.name, String(v.id))));
    const wanted = window.LMUrl && LMUrl.get("cview");
    currentView = views.find(v => String(v.id) === wanted) || null;
    sel.value = currentView ? String(currentView.id) : "";
    $("cv-view-delete").hidden = !currentView;
  }
  let viewDirty = false;
  function markViewDirty() { viewDirty = true; markUrl(); }
  function layoutNow() {
    return { lanes: lanes.map(l => ({ name: l.name, height: l.height })), range: [view.start, view.end] };
  }
  async function saveView() {
    const name = (currentView && currentView.name) || "";
    const form = document.createElement("form");
    form.className = "cv-pop";
    form.innerHTML = `<label>视图名称 <input name="n" maxlength="80" required value="${esc(name)}"></label>
      <div class="cv-pop-acts"><button class="btn small primary" type="submit">保存</button><button class="btn small ghost" type="button" data-x>取消</button></div>
      <p class="submuted">按变量名保存泳道、高度和时间范围；同名即覆盖。项目成员共享。</p>`;
    $("cv-view-save").after(form);
    form.querySelector("input").focus();
    form.querySelector("[data-x]").addEventListener("click", () => form.remove());
    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      const n = form.querySelector("input").value.trim();
      if (!n || !lanes.length) return;
      try {
        const existing = views.find(v => v.name === n);
        const body = { name: n, layout: layoutNow() };
        const data = existing ? await LMApi.updateCurveView(task.projectId, existing.id, body)
          : await LMApi.createCurveView(task.projectId, Object.assign({ test_id: ctx.task.test_id }, body));
        form.remove();
        if (window.LMUrl) LMUrl.replace({ cview: data.view.id });
        await loadViews(); viewDirty = false; markUrl();
        status("视图「" + n + "」已保存。");
      } catch (error) { status(error.message); }
    });
  }
  let deleteArmed = false;
  async function deleteView() {
    if (!currentView) return;
    if (!deleteArmed) {
      deleteArmed = true; $("cv-view-delete").textContent = "确认删除？";
      setTimeout(() => { deleteArmed = false; $("cv-view-delete").textContent = "删除视图"; }, 3000);
      return;
    }
    deleteArmed = false; $("cv-view-delete").textContent = "删除视图";
    try {
      await LMApi.deleteCurveView(task.projectId, currentView.id);
      if (window.LMUrl) LMUrl.replace({ cview: null });
      await loadViews(); status("视图已删除。");
    } catch (error) { status(error.message); }
  }

  // ----------------------------------------------------------------- URL
  function markUrl() {
    clearTimeout(urlTimer);
    urlTimer = setTimeout(() => {
      if (!window.LMUrl || !ctx || panel.hidden) return;
      LMUrl.replace({
        cv: 1, crun: ctx.task.run !== ctx.task.current_run ? ctx.task.run : null,
        cview: currentView && !viewDirty ? currentView.id : null,
        clanes: (currentView && !viewDirty) || lanes.map(l => l.name).join(",") === defaultKey ? null : lanes.map(l => l.name).join(","),
        cr: view ? [view.start, view.end].map(v => Number(v.toFixed(6))).join("~") : null,
        ca: cursors.a !== null ? Number(cursors.a.toFixed(6)) : null,
        cb: cursors.b !== null ? Number(cursors.b.toFixed(6)) : null,
        ccmp: compare ? compare.key : null,
      });
    }, 250);
  }
  function restoreFromUrl() {
    if (!window.LMUrl || LMUrl.get("cv") !== "1") return null;
    const range = (LMUrl.get("cr") || "").split("~").map(Number);
    return {
      run: Number(LMUrl.get("crun")) || null,
      lanes: LMUrl.get("clanes") ? LMUrl.get("clanes").split(",").filter(Boolean) : null,
      range: range.length === 2 && range.every(Number.isFinite) ? range : null,
      compare: LMUrl.get("ccmp") || null,
      a: Number.isFinite(parseFloat(LMUrl.get("ca"))) ? parseFloat(LMUrl.get("ca")) : null,
      b: Number.isFinite(parseFloat(LMUrl.get("cb"))) ? parseFloat(LMUrl.get("cb")) : null,
    };
  }
  function clearUrl() {
    if (window.LMUrl) LMUrl.replace({ cv: null, crun: null, cview: null, clanes: null, cr: null, ca: null, cb: null, ccmp: null });
  }

  // -------------------------------------------------------------- export
  function download(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function exportName(ext) {
    return `${ctx.task.test_id}_run${ctx.task.run}_${R.fmtValue(Number(view.start.toFixed(3)))}-${R.fmtValue(Number(view.end.toFixed(3)))}s.${ext}`.replace(/[^\w.\-]+/g, "_");
  }
  function exportImage(kind) {
    if (!lastModel || !lanes.length) { status("没有可导出的泳道。"); return; }
    const m = Object.assign({}, lastModel, { hoverX: null });
    if (kind === "svg") {
      const p = R.svgPainter(m.width, m.height, document.createElement("canvas").getContext("2d"));
      R.render(p, m);
      download(new Blob([p.done()], { type: "image/svg+xml" }), exportName("svg"));
      return;
    }
    const scale = 2, off = document.createElement("canvas");
    off.width = m.width * scale; off.height = m.height * scale;
    const g = off.getContext("2d"); g.setTransform(scale, 0, 0, scale, 0, 0);
    R.render(R.canvasPainter(g), m);
    off.toBlob(b => download(b, exportName("png")), "image/png");
  }
  function exportCsv() {
    if (!lanes.length) { status("先选择变量。"); return; }
    location.href = LMApi.taskCurvesUrl(task.projectId, task.taskKey, "/export",
      params({ columns: lanes.map(l => l.id).join(","), start: view.start, end: view.end }));
  }

  // ------------------------------------------------------------ lower panes
  function loadLower() {
    if (!ctx) return;
    if (lower === "events") loadEvents();
    else if (lower === "checks") renderChecks();
    else loadRaw();
  }
  async function loadEvents() {
    const q = params({ offset: eventsOffset, limit: 200 });
    if ($("cv-events-lanes").checked && lanes.length) q.columns = lanes.map(l => l.id).join(",");
    if ($("cv-events-range").checked) { q.start = view.start; q.end = view.end; }
    try {
      const data = await api("/events", q);
      if (!data) return;
      const table = $("cv-events");
      table.innerHTML = "<thead><tr><th scope=\"col\">时间 (s)</th><th scope=\"col\">记录</th><th scope=\"col\">变量</th><th scope=\"col\">从</th><th scope=\"col\">到</th><th scope=\"col\">步骤</th></tr></thead>";
      const body = document.createElement("tbody");
      data.events.forEach(e => {
        const c = columnById(e.id), s = stepAt(e.t);
        const tr = document.createElement("tr"); tr.tabIndex = 0;
        tr.innerHTML = `<td class="num">${esc(R.fmtValue(e.t))}</td><td class="num">${e.row}</td><td title="${esc(c.name)}">${esc(display(c))}</td><td>${esc(valueLabel(c, e.from))}</td><td>${esc(valueLabel(c, e.to))}</td><td>${s ? "Step" + esc(s.step) : ""}</td>`;
        const go = () => { ensureLanes([c.name]); setCursor("a", e.t); ensureVisible(e.t); };
        tr.addEventListener("click", go);
        tr.addEventListener("keydown", ev => { if (ev.isComposing || ev.keyCode === 229) return; if (ev.key === "Enter") go(); });
        body.append(tr);
      });
      table.append(body);
      $("cv-events-info").textContent = data.total ? `${eventsOffset + 1}–${eventsOffset + data.events.length} / ${data.total} 个跳变` : "没有跳变";
      $("cv-events-prev").disabled = eventsOffset === 0;
      $("cv-events-next").disabled = eventsOffset + data.events.length >= data.total;
      if (!density) loadDensity();
    } catch (error) { status(error.message); }
  }
  function renderChecks() {
    const table = $("cv-checks");
    table.innerHTML = "<thead><tr><th scope=\"col\">步骤</th><th scope=\"col\">结果</th><th scope=\"col\">时刻 (s)</th><th scope=\"col\">变量</th><th scope=\"col\">期望</th><th scope=\"col\">实测</th><th scope=\"col\">确认时机</th></tr></thead>";
    const body = document.createElement("tbody");
    (ctx.judge.steps || []).forEach(s => s.checks.forEach(c => {
      const tr = document.createElement("tr"); tr.tabIndex = 0;
      tr.className = c.ok ? "" : "cv-bad";
      const inCsv = c.var && columnByName(c.var);
      tr.innerHTML = `<td>Step${esc(s.step)}${c.subroutine ? `<div class="submuted">${esc(c.subroutine)} · ${esc(c.subroutine_step)}</div>` : ""}</td>
        <td>${c.ok ? '<span class="jbadge pass">✓</span>' : '<span class="jbadge fail">✕</span>'}</td>
        <td class="num">${c.at === null ? "—" : esc(R.fmtValue(c.at))}</td>
        <td title="${esc(c.var || "")}">${esc(c.name || c.var || "")}${inCsv ? "" : '<div class="submuted">未记录在 output.csv</div>'}</td>
        <td>${esc(c.expected || "")}</td><td>${esc(c.observed || "")}</td><td>${esc(c.timing || "")}</td>`;
      const go = () => { focusStep = s; if (inCsv) ensureLanes([c.var]); if (c.at !== null) { setCursor("a", c.at); ensureVisible(c.at); } };
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", ev => { if (ev.isComposing || ev.keyCode === 229) return; if (ev.key === "Enter") go(); });
      body.append(tr);
    }));
    if (!body.children.length) body.innerHTML = '<tr><td colspan="7" class="muted">没有判定检查点。</td></tr>';
    table.append(body);
  }
  async function loadRaw() {
    if (!ctx.can_download) { $("cv-raw").innerHTML = '<tbody><tr><td class="muted">没有下载权限，无法查看原始数据。</td></tr></tbody>'; return; }
    try {
      const page = await api("/raw", params({ offset: rawOffset, count: 100 }));
      if (!page) return;
      const head = document.createElement("thead"), tr = document.createElement("tr");
      ["记录", ...page.header].forEach(n => { const th = document.createElement("th"); th.textContent = n; tr.append(th); });
      head.append(tr);
      const body = document.createElement("tbody");
      page.rows.forEach(r => {
        const row = document.createElement("tr");
        [r.row, ...r.cells].forEach(v => { const td = document.createElement("td"); td.textContent = v; row.append(td); });
        body.append(row);
      });
      $("cv-raw").replaceChildren(head, body);
      $("cv-raw-page").textContent = page.rows.length ? `记录 ${rawOffset + 1}–${rawOffset + page.rows.length}` : "无记录";
      $("cv-raw-prev").disabled = rawOffset === 0; $("cv-raw-next").disabled = !page.has_more;
    } catch (error) { status(error.message); }
  }

  // ------------------------------------------------------------ pointer
  function laneAtY(y) {
    if (!lastModel) return -1;
    return lastModel.lanes.findIndex(l => y >= l.y && y < l.y + l.height);
  }
  let drag = null;
  canvas.addEventListener("pointerdown", ev => {
    if (!ctx) return;
    canvas.focus();
    const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
    const i = laneAtY(y);
    if (x < LABEL_W && i >= 0) {
      const lane = lastModel.lanes[i];
      if (x > LABEL_W - 26 && y < lane.y + 18) { // remove ×
        lanes.splice(i, 1); focusLane = -1; markViewDirty(); tree(); requestDraw(true); return;
      }
      const edge = Math.abs(y - (lane.y + lane.height)) <= 4;
      drag = { kind: edge ? "resize" : "move", index: i, y0: y, h0: lanes[i].height, moved: false };
    } else if (y < R.AXIS_H + R.BAND_H && y >= R.AXIS_H && x >= LABEL_W) {
      const t = timeAtX(x), s = stepAt(t);
      if (s) focusOnStep(s.step);
      return;
    } else {
      drag = { kind: "pan", x0: x, view0: Object.assign({}, view), moved: false, shift: ev.shiftKey };
    }
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener("pointermove", ev => {
    if (!ctx) return;
    const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
    if (drag) {
      if (drag.kind === "pan") {
        const dx = x - drag.x0;
        if (Math.abs(dx) > 3) drag.moved = true;
        if (drag.moved) {
          const dt = dx / plotWidth() * (drag.view0.end - drag.view0.start);
          view = clampView(drag.view0.start - dt, drag.view0.end - dt);
          requestDraw(false);
        }
      } else if (drag.kind === "resize") {
        lanes[drag.index].height = Math.max(24, Math.min(400, Math.round(drag.h0 + y - drag.y0)));
        drag.moved = true; requestDraw(false);
      } else {
        const target = laneAtY(y);
        if (Math.abs(y - drag.y0) > 4) drag.moved = true;
        if (drag.moved && target >= 0 && target !== drag.index) {
          const [moved] = lanes.splice(drag.index, 1);
          lanes.splice(target, 0, moved); drag.index = target; focusLane = target; requestDraw(false);
        }
      }
      return;
    }
    canvas.style.cursor = x < LABEL_W ? (laneAtY(y) >= 0 && lastModel && Math.abs(y - (lastModel.lanes[laneAtY(y)].y + lastModel.lanes[laneAtY(y)].height)) <= 4 ? "ns-resize" : "grab") : "crosshair";
    hover(x, y);
  });
  canvas.addEventListener("pointerup", ev => {
    if (!drag) return;
    const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left;
    const d = drag; drag = null;
    if (d.kind === "pan") {
      if (!d.moved) setCursor(d.shift ? "b" : "a", timeAtX(x));
      else { markUrl(); requestDraw(true); }
    } else if (d.kind === "move" && !d.moved) {
      focusLane = focusLane === d.index ? -1 : d.index; requestDraw(false);
    } else { markViewDirty(); }
  });
  canvas.addEventListener("pointerleave", () => { hoverX = null; tip.hidden = true; requestDraw(false); });
  canvas.addEventListener("wheel", ev => {
    if (!ctx) return;
    ev.preventDefault();
    const r = canvas.getBoundingClientRect(), x = Math.max(LABEL_W, ev.clientX - r.left);
    const span = view.end - view.start;
    if (ev.shiftKey || Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {
      const dt = (ev.deltaX || ev.deltaY) / plotWidth() * span;
      setView(view.start + dt, view.end + dt); return;
    }
    const t = timeAtX(x), k = Math.exp(ev.deltaY * 0.0015);
    setView(t - (t - view.start) * k, t + (view.end - t) * k);
  }, { passive: false });
  canvas.addEventListener("dblclick", ev => {
    const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
    if (y >= R.AXIS_H && y < R.AXIS_H + R.BAND_H && x >= LABEL_W) {
      const s = stepAt(timeAtX(x));
      if (s) document.dispatchEvent(new CustomEvent("lm-judge-focus-step", { detail: { step: s.step } }));
    }
  });

  function hover(x, y) {
    if (x < LABEL_W || !lastModel) { hoverX = null; tip.hidden = true; requestDraw(false); return; }
    hoverX = x; requestDraw(false);
    const t = timeAtX(x), s = stepAt(t), i = laneAtY(y);
    const rows = [];
    rows.push(`<div class="cv-tip-h"><b>${esc(R.fmtValue(Number(t.toFixed(6))))} s</b>${s ? ` · Step${esc(s.step)} ${esc(s.result || "")}` : ""}</div>`);
    if (s && y < R.AXIS_H + R.BAND_H) rows.push(`<div class="cv-tip-t">${esc(s.title)}</div><div class="submuted">单击放大到该步骤 · 双击在判定结果中定位</div>`);
    lastModel.lanes.forEach((l, k) => {
      const c = columnById(l.id);
      let text = "…";
      if (l.series && l.series.mode === "exact") text = valueLabel(c, R.valueAt(l.series.points, t));
      else if (l.series) {
        const b = l.series.buckets.filter(q => q[0] <= t).pop();
        text = b ? R.fmtValue(b[3]) + "–" + R.fmtValue(b[4]) + "（像素内范围）" : "—";
      }
      let theirs = "";
      if (l.other && l.other.mode === "exact") {
        const ov = R.valueAt(l.other.points, t);
        theirs = ` <span class="cv-tip-o">对比 ${esc(valueLabel(c, ov))}</span>`;
      }
      rows.push(`<div class="cv-tip-r${k === i ? " on" : ""}"><i style="background:${l.color}"></i><span>${esc(l.display)}</span><b>${esc(text)}</b>${theirs}</div>`);
    });
    if (i >= 0) {
      const lane = lastModel.lanes[i];
      lane.checks.filter(c => c.at !== null && Math.abs(xAtTime(c.at) - x) <= 7).forEach(c => {
        rows.push(`<div class="cv-tip-c ${c.ok ? "ok" : "ng"}">${c.ok ? "✓" : "✕"} ${esc(c.name)} @${esc(R.fmtValue(c.at))} s<br>期望 ${esc(c.expected)}<br>实测 ${esc(c.observed)} · ${esc(c.timing || "")}</div>`);
      });
    }
    tip.innerHTML = rows.join("");
    tip.hidden = false;
    const wrapRect = wrap.getBoundingClientRect();
    const left = x + 16 + tip.offsetWidth > wrapRect.width ? x - tip.offsetWidth - 12 : x + 16;
    tip.style.left = Math.max(4, left) + "px";
    tip.style.top = Math.max(4, Math.min(y + 12, canvas.clientHeight - tip.offsetHeight - 4)) + "px";
  }

  canvas.addEventListener("keydown", ev => {
    if (!ctx) return;
    const span = view.end - view.start;
    const key = ev.key;
    const handled = () => ev.preventDefault();
    if (key === "ArrowRight" || key === "ArrowLeft") { handled(); jumpEdge(ev.shiftKey ? "b" : "a", key === "ArrowRight" ? 1 : -1); }
    else if (key === "ArrowDown" || key === "ArrowUp") {
      handled(); if (!lanes.length) return;
      focusLane = Math.max(0, Math.min(lanes.length - 1, focusLane + (key === "ArrowDown" ? 1 : -1))); requestDraw(false);
    } else if (key === "+" || key === "=") { handled(); const c = (view.start + view.end) / 2; setView(c - span / 4, c + span / 4); }
    else if (key === "-") { handled(); const c = (view.start + view.end) / 2; setView(c - span, c + span); }
    else if (key === "0") { handled(); setView(ctx.domain[0], ctx.domain[1]); }
    else if (key === "f" || key === "F") { handled(); firstFailure(); }
    else if ((key === "Delete" || key === "Backspace") && focusLane >= 0) {
      handled(); lanes.splice(focusLane, 1); focusLane = -1; markViewDirty(); tree(); requestDraw(true);
    } else if (key === "Escape" && panel.classList.contains("cv-full")) { handled(); fullscreen(false); }
  });

  // minimap drag
  let mdrag = null;
  mini.addEventListener("pointerdown", ev => {
    if (!ctx) return;
    const r = mini.getBoundingClientRect(), x = ev.clientX - r.left;
    const W = r.width - 8 - LABEL_W, dom = ctx.domain, span = (dom[1] - dom[0]) || 1;
    const tAt = px => dom[0] + (px - LABEL_W) / W * span;
    const a = LABEL_W + (view.start - dom[0]) / span * W, b = LABEL_W + (view.end - dom[0]) / span * W;
    let kind = "move";
    if (Math.abs(x - a) <= 5) kind = "left"; else if (Math.abs(x - b) <= 5) kind = "right";
    else if (x < a || x > b) { const s = view.end - view.start, t = tAt(x); setView(t - s / 2, t + s / 2); }
    mdrag = { kind, x0: x, v0: Object.assign({}, view), tAt, perPx: span / W };
    mini.setPointerCapture(ev.pointerId);
  });
  mini.addEventListener("pointermove", ev => {
    if (!mdrag) return;
    const r = mini.getBoundingClientRect(), x = ev.clientX - r.left, dt = (x - mdrag.x0) * mdrag.perPx;
    if (mdrag.kind === "move") view = clampView(mdrag.v0.start + dt, mdrag.v0.end + dt);
    else if (mdrag.kind === "left") view = clampView(Math.min(mdrag.v0.start + dt, mdrag.v0.end - 1e-6), mdrag.v0.end);
    else view = clampView(mdrag.v0.start, Math.max(mdrag.v0.end + dt, mdrag.v0.start + 1e-6));
    requestDraw(false);
  });
  mini.addEventListener("pointerup", () => { if (mdrag) { mdrag = null; markUrl(); requestDraw(true); } });

  // ------------------------------------------------------------ controls
  function fullscreen(on) {
    panel.classList.toggle("cv-full", on);
    document.body.classList.toggle("cv-noscroll", on);
    $("cv-full").setAttribute("aria-pressed", String(on));
    $("cv-full").textContent = on ? "退出全屏" : "全屏";
    requestDraw(true);
  }
  $("cv-full").addEventListener("click", () => fullscreen(!panel.classList.contains("cv-full")));
  $("cv-fit").addEventListener("click", () => ctx && setView(ctx.domain[0], ctx.domain[1]));
  $("cv-clear-a").addEventListener("click", () => ctx && setCursor("a", null));
  $("cv-clear-b").addEventListener("click", () => ctx && setCursor("b", null));
  $("cv-first-fail").addEventListener("click", firstFailure);
  $("cv-run").addEventListener("change", () => { const n = Number($("cv-run").value); load(n, null); });
  $("cv-compare").addEventListener("change", () => setCompare($("cv-compare").value));
  $("cv-view").addEventListener("change", () => {
    const v = views.find(x => String(x.id) === $("cv-view").value) || null;
    currentView = v; viewDirty = false;
    $("cv-view-delete").hidden = !v;
    if (v) applyLayout(v.layout); else defaultLanes();
    if (window.LMUrl) LMUrl.replace({ cview: v ? v.id : null });
    tree(); markUrl(); requestDraw(true); refreshCursor();
  });
  $("cv-view-save").addEventListener("click", saveView);
  $("cv-view-delete").addEventListener("click", deleteView);
  $("cv-link").addEventListener("click", async () => {
    markUrl();
    setTimeout(async () => {
      try { await navigator.clipboard.writeText(location.href); status("已复制当前视图链接。"); }
      catch (_e) { status("复制失败；请直接复制浏览器地址栏中的链接。"); }
    }, 300);
  });
  // The export dropdown advertises role="menu", so it follows the ARIA menu button
  // pattern: the trigger is the single tab stop, opening moves focus into the list,
  // arrow/Home/End keys move within it, Escape closes and restores focus, and Tab
  // closes without being trapped so focus continues from the trigger.
  const menuBtn = $("cv-export"), menuList = $("cv-export-menu");
  // Only items the user can actually reach take part in the arrow sequence. The
  // download entry stays in the DOM while it has no address or no permission, so
  // without this filter a wrapped step lands on it and focus() becomes a no-op.
  const menuItems = () => Array.from(menuList.querySelectorAll('[role="menuitem"]'))
    .filter(item => !item.hidden && !item.disabled && item.getAttribute("aria-disabled") !== "true");
  const menuOpen = () => !menuList.hidden;
  function openMenu(first) {
    menuList.hidden = false; menuBtn.setAttribute("aria-expanded", "true");
    const item = first || menuItems()[0];
    if (item) item.focus();
  }
  function closeMenu(restore, clickTarget) {
    // Clicking a non-focusable area blurs the menuitem, so by the time the
    // document click listener runs the focus is either still inside the menu
    // or has fallen back to the page. Both mean it was in content that is
    // about to hide. A click that landed on another control is left alone.
    const active = document.activeElement;
    const lostToPage = !active || active === document.body;
    const inside = menuList.contains(active);
    // Safari leaves the focus where it was when a <button> is clicked, so a
    // click on another control also reads as "lost to the page" there. That
    // control owns the focus decision; do not pull it back to the trigger.
    const clickedControl = clickTarget && clickTarget.closest
      && clickTarget.closest("button, a, input, select, textarea, label, summary");
    menuList.hidden = true; menuBtn.setAttribute("aria-expanded", "false");
    if (restore || inside || (lostToPage && !clickedControl)) menuBtn.focus();
  }
  function stepMenu(key) {
    const items = menuItems();
    if (!items.length) return;
    const at = items.indexOf(document.activeElement);
    let next;
    if (key === "Home") next = 0;
    else if (key === "End") next = items.length - 1;
    else if (key === "ArrowDown") next = at < 0 ? 0 : (at + 1) % items.length;
    else next = at <= 0 ? items.length - 1 : at - 1;
    items[next].focus();
  }
  menuBtn.addEventListener("click", () => { if (menuOpen()) closeMenu(true); else openMenu(); });
  menuBtn.addEventListener("keydown", event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const items = menuItems();
      openMenu(event.key === "ArrowDown" ? items[0] : items[items.length - 1]);
    } else if (event.key === "Escape" && menuOpen()) {
      event.preventDefault(); closeMenu(true);
    }
  });
  menuList.addEventListener("keydown", event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "Escape") { event.preventDefault(); closeMenu(true); return; }
    // Tab leaves the menu; closing from here and refocusing the trigger keeps focus
    // out of hidden content and lets the browser continue the Tab order from it.
    // Shift+Tab must still take this path, so Shift is not part of the guard above.
    if (event.key === "Tab") { closeMenu(true); return; }
    // Buttons activate on Enter and Space by themselves; a <a role="menuitem">
    // does not, and Space would scroll the page instead.
    const active = document.activeElement;
    if ((event.key === " " || event.key === "Enter") && active && active.tagName === "A") {
      event.preventDefault(); active.click(); return;
    }
    if (event.shiftKey) return;
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault(); stepMenu(event.key);
    }
  });
  menuList.addEventListener("click", event => {
    const picked = event.target.closest("[data-export]");
    closeMenu(true);
    if (!picked) return;
    if (picked.dataset.export === "csv") exportCsv(); else exportImage(picked.dataset.export);
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".cv-menu") && menuOpen()) closeMenu(false, event.target);
  });
  $("cv-search").addEventListener("input", tree);
  document.querySelectorAll(".cv-chip").forEach(b => b.addEventListener("click", () => { filter = b.dataset.filter; syncFilterChips(); tree(); }));
  // The lower tablist shares one activation path for clicks and arrow keys, so
  // aria-selected, the roving tabindex, the visible pane and the data load can
  // never drift apart.
  function activateLower(tab) {
    lower = tab.dataset.lower;
    document.querySelectorAll("[data-lower]").forEach(t => { const on = t === tab; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; });
    document.querySelectorAll("[data-pane]").forEach(p => { p.hidden = p.dataset.pane !== lower; });
    loadLower();
  }
  document.querySelectorAll("[data-lower]").forEach((tab, i, tabs) => {
    tab.addEventListener("click", () => activateLower(tab));
    tab.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : event.key === "ArrowRight" ? (i + 1) % tabs.length : (i + tabs.length - 1) % tabs.length;
      activateLower(tabs[next]);
      tabs[next].focus();
    });
  });
  ["cv-events-lanes", "cv-events-range"].forEach(id => $(id).addEventListener("change", () => { eventsOffset = 0; loadEvents(); }));
  $("cv-events-prev").addEventListener("click", () => { eventsOffset = Math.max(0, eventsOffset - 200); loadEvents(); });
  $("cv-events-next").addEventListener("click", () => { eventsOffset += 200; loadEvents(); });
  $("cv-raw-prev").addEventListener("click", () => { rawOffset = Math.max(0, rawOffset - 100); loadRaw(); });
  $("cv-raw-next").addEventListener("click", () => { rawOffset += 100; loadRaw(); });
  new ResizeObserver(() => { if (ctx && !panel.hidden) requestDraw(true); }).observe(wrap);
  new MutationObserver(() => { if (ctx) requestDraw(false); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  let eventsTimer = 0;
  const refreshEvents = () => { clearTimeout(eventsTimer); eventsTimer = setTimeout(() => { if (lower === "events" && $("cv-events-range").checked) { eventsOffset = 0; loadEvents(); } }, 400); };
  ["pointerup", "wheel"].forEach(t => canvas.addEventListener(t, refreshEvents));

  // ---------------------------------------------------------------- tabs
  function activate(curves, push) {
    [$("lm-result-tab"), $("lm-curves-tab")].forEach((tab, i) => {
      const on = Boolean(i) === curves;
      tab.setAttribute("aria-selected", String(on)); tab.tabIndex = on ? 0 : -1;
    });
    $("lm-result-content").hidden = curves; panel.hidden = !curves;
    if (!curves && panel.classList.contains("cv-full")) fullscreen(false);
    if (push !== false && window.LMUrl) LMUrl.replace({ cv: curves ? 1 : null });
    if (curves) { if (!ctx) load(null, restoreFromUrl()); else requestDraw(true); }
  }
  [$("lm-result-tab"), $("lm-curves-tab")].forEach((tab, i, tabs) => {
    tab.addEventListener("click", () => activate(Boolean(i)));
    tab.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - i;
      activate(Boolean(next)); tabs[next].focus();
    });
  });

  document.addEventListener("lm-curves-focus-step", ev => {
    const step = ev.detail.step;
    activate(true);
    const tryFocus = () => { if (ctx) focusOnStep(step); else setTimeout(tryFocus, 150); };
    tryFocus();
  });
  document.addEventListener("lm-task-open", ev => {
    reset(); task = ev.detail;
    status("尚未读取 output.csv。");
    const restore = restoreFromUrl();
    if (restore) { activate(true, false); }
    else if (!panel.hidden) load(null, null);
  });
  document.addEventListener("lm-task-close", () => {
    reset(); task = null; clearUrl();
    if (panel.classList.contains("cv-full")) fullscreen(false);
  });
})();
