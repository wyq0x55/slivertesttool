// Covers waveform interactions ported to the Variable Curves panel:
// right-click B cursor, direct cursor dragging, cursor measurement table with
// signed (right - left) delta and row selection, A-B zoom, time-axis drag zoom,
// and the native <dialog> batch variable selector.
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const root = path.resolve(__dirname, "../..");
const template = fs.readFileSync(
  path.join(root, "app/templates/lanmatrix/project_tasks.html"), "utf8");
const renderSource = fs.readFileSync(
  path.join(root, "app/static/js/lanmatrix/curve_render.js"), "utf8");
const curvesSource = fs.readFileSync(
  path.join(root, "app/static/js/lanmatrix/task_curves.js"), "utf8");

function makeCanvasContext() {
  const calls = [];
  return {
    calls,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    font: "",
    textAlign: "left",
    textBaseline: "middle",
    setTransform() {},
    save() {},
    restore() {},
    beginPath() {},
    closePath() {},
    rect() {},
    clip() {},
    moveTo() {},
    lineTo() {},
    setLineDash() {},
    stroke() { calls.push({ op: "stroke", style: this.strokeStyle, width: this.lineWidth }); },
    fill() { calls.push({ op: "fill", style: this.fillStyle }); },
    fillRect(x, y, w, h) { calls.push({ op: "fillRect", x, y, w, h, style: this.fillStyle }); },
    strokeRect(x, y, w, h) { calls.push({ op: "strokeRect", x, y, w, h, style: this.strokeStyle }); },
    fillText(text, x, y) { calls.push({ op: "fillText", text, x, y }); },
    measureText(text) { return { width: String(text).length * 7 }; },
  };
}

function makeElement(id, doc, extra) {
  const ctx2d = makeCanvasContext();
  const element = {
    id,
    tagName: "DIV",
    hidden: false,
    disabled: false,
    open: false,
    checked: false,
    value: "",
    textContent: "",
    innerHTML: "",
    tabIndex: 0,
    clientWidth: 848, // LABEL_W(240) + plot(600) + right pad(8)
    clientHeight: 300,
    offsetWidth: 120,
    offsetHeight: 60,
    style: {},
    dataset: {},
    options: [],
    children: [],
    attributes: new Map(),
    listeners: new Map(),
    classList: {
      set: new Set(),
      add(n) { this.set.add(n); },
      remove(n) { this.set.delete(n); },
      contains(n) { return this.set.has(n); },
      toggle(n, on) {
        if (on === undefined) this.set.has(n) ? this.set.delete(n) : this.set.add(n);
        else if (on) this.set.add(n); else this.set.delete(n);
      },
    },
    addEventListener(type, fn) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(fn);
    },
    setAttribute(name, value) { this.attributes.set(name, String(value)); },
    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; },
    replaceChildren(...nodes) { this.children = [...nodes]; this.options = [...nodes]; this.innerHTML = ""; },
    append(...nodes) { this.children.push(...nodes); },
    add(opt) { this.options.push(opt); this.children.push(opt); },
    querySelectorAll() { return []; },
    closest() { return null; },
    contains(node) { return node === this; },
    focus() { doc.activeElement = this; },
    getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; },
    setPointerCapture() {},
    getContext() { return ctx2d; },
    _ctx2d: ctx2d,
    showModal() { this.open = true; },
    close(returnValue) { this.open = false; if (returnValue !== undefined) this.returnValue = returnValue; },
  };
  return Object.assign(element, extra);
}

function fire(target, type, props) {
  const event = Object.assign({
    type,
    target,
    button: 0,
    pointerId: 1,
    clientX: 0,
    clientY: 0,
    shiftKey: false,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
  }, props);
  for (const fn of target.listeners.get(type) || []) fn(event);
  return event;
}

function createHarness({ books = {}, loadSavedViews } = {}) {
  const elements = new Map();
  const docListeners = new Map();
  const doc = {
    activeElement: null,
    documentElement: makeElement("html", null),
    body: makeElement("body", null),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id, doc));
      return elements.get(id);
    },
    createElement(tag) {
      return makeElement("", doc, { tagName: tag.toUpperCase() });
    },
    querySelectorAll() { return []; },
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push(fn);
    },
    dispatch(type, detail) {
      const ev = { type, detail };
      for (const fn of docListeners.get(type) || []) fn(ev);
    },
  };

  // Pre-seed controls with specific defaults
  elements.set("lm-curves-content", makeElement("lm-curves-content", doc, { hidden: false }));
  elements.set("cv-canvas", makeElement("cv-canvas", doc, { tagName: "CANVAS", clientWidth: 848, clientHeight: 240 }));
  elements.set("cv-minimap", makeElement("cv-minimap", doc, { tagName: "CANVAS", clientWidth: 848, clientHeight: 44 }));
  elements.set("cv-wrap", makeElement("cv-wrap", doc, { clientWidth: 848, clientHeight: 240 }));
  elements.set("cv-batch", makeElement("cv-batch", doc, { tagName: "DIALOG", open: false, closedBy: "any" }));
  elements.set("cv-measurements", makeElement("cv-measurements", doc, { tagName: "DETAILS", hidden: true, open: false }));
  elements.set("cv-zoom-cursors", makeElement("cv-zoom-cursors", doc, { tagName: "BUTTON", disabled: true }));

  const urlState = {};
  const baseContext = {
    revision: "rev1",
    rows: 1001,
    domain: [0, 10],
    sample_interval: 0.01,
    time_unit: "s",
    live: false,
    can_download: true,
    attempts: [1],
    task: { run: 1, current_run: 1, test_id: "TC_01" },
    limits: { lanes: 16 },
    columns: [
      { id: "c1", index: 1, name: "m_ACT_MOT_SPD", kind: "analog", changes: 4, gaps: 0, initial: 0, min: 0, max: 100, integer: false, values: null },
      { id: "c2", index: 2, name: "m_ACT_MOT_TRQ", kind: "analog", changes: 3, gaps: 0, initial: 10, min: 10, max: 50, integer: false, values: null },
      { id: "c3", index: 3, name: "sig_DOOR_SW", kind: "discrete", changes: 2, gaps: 0, initial: 0, min: 0, max: 1, integer: true, values: [0, 1] },
    ],
    dictionary: {
      source: "procedure",
      signals: {
        m_ACT_MOT_SPD: { name: "MotorSpeed", roles: ["check"], labels: {} },
        m_ACT_MOT_TRQ: { name: "MotorTorque", roles: ["input"], labels: {} },
        sig_DOOR_SW: { name: "DriverDoor", roles: [], labels: { "0": ["CLOSED"], "1": ["OPEN"] } },
      },
    },
    judge: { available: true, verdict: "PASS", first_failure: null, steps: [] },
  };

  // Signal values as a function of time:
  // c1 = 10 * t, c2 = 50 - 2 * t, c3 = t >= 4 ? 1 : 0
  const valueFor = (id, t) => {
    if (id === "c1") return Math.round(10 * t * 100) / 100;
    if (id === "c2") return Math.round((50 - 2 * t) * 100) / 100;
    if (id === "c3") return t >= 4 ? 1 : 0;
    return null;
  };

  const requests = [];
  const LMApi = {
    taskCurves(_pid, taskKey, part, query) {
      requests.push({ part, query });
      if (part === "") return Promise.resolve({
        ...baseContext, task: { ...baseContext.task, book_id: books[taskKey] },
      });
      if (part === "/series") {
        const cols = String(query.columns || "").split(",").filter(Boolean);
        return Promise.resolve({
          revision: "rev1", range: [0, 10], domain: [0, 10],
          series: cols.map(id => ({
            id,
            kind: id === "c3" ? "discrete" : "analog",
            mode: "exact",
            points: [[0, valueFor(id, 0), 1], [5, valueFor(id, 5), 501], [10, valueFor(id, 10), 1001]],
          })),
        });
      }
      if (part === "/values") {
        const cols = String(query.columns || "").split(",").filter(Boolean);
        const at = Number(query.at);
        const values = {};
        for (const id of cols) values[id] = valueFor(id, at);
        return Promise.resolve({ revision: "rev1", t: at, row: Math.round(at * 100) + 1, values });
      }
      if (part === "/events") return Promise.resolve({ events: [], total: 0 });
      return Promise.resolve({});
    },
    taskCurvesUrl: () => "#download",
    listProjectTasks() { return Promise.resolve({ tasks: [] }); },
    curveViews(projectId, testId, bookId) {
      if (loadSavedViews) return loadSavedViews(projectId, testId, bookId);
      return Promise.resolve({
        views: [{ id: 7, name: "DoorView", layout: { lanes: [{ name: "sig_DOOR_SW", height: 30 }] } }],
      });
    },
    createCurveView() { return Promise.resolve({}); },
    updateCurveView() { return Promise.resolve({}); },
    deleteCurveView() { return Promise.resolve({}); },
  };

  const cssVars = {
    "--surface": "#ffffff",
    "--surface-2": "#f6f7f9",
    "--border": "#d0d7de",
    "--cv-grid": "#e5e7eb",
    "--cv-zebra": "#f9fafb",
    "--text": "#111827",
    "--text-2": "#4b5563",
    "--text-3": "#9ca3af",
    "--brand": "#5e6ad2",
    "--brand-soft": "#eef0ff",
    "--brand-text": "#3f4ab0",
    "--ok": "#16a34a",
    "--ok-soft": "#dcfce7",
    "--cv-ok-strong": "#86efac",
    "--danger": "#dc2626",
    "--danger-soft": "#fee2e2",
    "--warn": "#d97706",
    "--cv-ghost": "#6b7280",
    "--cv-fill": "rgba(94,106,210,.10)",
    "--cv-shade": "rgba(0,0,0,.08)",
    "--cv-density": "#93c5fd",
  };

  class Observer { observe() {} disconnect() {} }
  function Option(text, value) { return { textContent: text, value: value === undefined ? text : String(value) }; }

  const sandbox = {
    document: doc,
    devicePixelRatio: 1,

    LMApi,
    AbortController,
    URLSearchParams,
    Option,
    location: { search: "", href: "http://localhost/lanmatrix/projects/1/tasks/T1?cv=1" },
    navigator: { clipboard: { writeText: async () => {} } },
    LMUrl: {
      get(key) { return urlState[key] ?? null; },
      replace(patch) { Object.assign(urlState, patch); },
    },
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval() {},
    requestAnimationFrame: fn => { fn(); return 0; },
    getComputedStyle: () => ({ getPropertyValue: name => cssVars[name] || "" }),
    ResizeObserver: Observer,
    MutationObserver: Observer,
  };
  sandbox.window = sandbox;

  const context = vm.createContext(sandbox);
  vm.runInContext(renderSource, context, { filename: "curve_render.js" });
  vm.runInContext(curvesSource, context, { filename: "task_curves.js" });

  async function openTask(taskKey = "T1") {
    doc.dispatch("lm-task-open", { projectId: 1, taskKey });
    // load() -> fillCompare() -> curveViews() -> values(); also let the
    // debounced markUrl() timer (250 ms) settle before reading urlState.
    await new Promise(resolve => setTimeout(resolve, 300));
  }

  return { doc, urlState, requests, openTask };
}

test("saved views follow the book from the opened task context", async () => {
  const calls = [];
  let resolveFirst;
  const first = new Promise(resolve => { resolveFirst = resolve; });
  const { doc, openTask } = createHarness({
    books: { T1: 10, T2: 20 },
    loadSavedViews(projectId, testId, bookId) {
      calls.push({ projectId, testId, bookId });
      return calls.length === 1 ? first : Promise.resolve({ views: [{ id: 20, name: "Second" }] });
    },
  });
  await openTask();
  await openTask("T2");
  resolveFirst({ views: [{ id: 10, name: "First" }] });
  await first;
  await Promise.resolve();
  assert.deepEqual(calls, [
    { projectId: 1, testId: "TC_01", bookId: 10 },
    { projectId: 1, testId: "TC_01", bookId: 20 },
  ]);
  assert.deepEqual(doc.getElementById("cv-view").options.map(option => option.value), ["", "20"]);
});

test("template exposes accessible batch dialog, A-B zoom control, and measurement table", () => {
  assert.match(template, /<dialog[^>]*id="cv-batch"[^>]*aria-labelledby="cv-batch-title"[^>]*closedby="any"/);
  assert.match(template, /<form[^>]*id="cv-batch-form"[^>]*method="dialog"/);
  assert.match(template, /id="cv-batch-fill"/);
  assert.match(template, /id="cv-zoom-cursors"[^>]*disabled/);
  assert.match(template, /<details[^>]*id="cv-measurements"[^>]*hidden/);
  assert.match(template, /<th scope="col">Δ值（右 − 左）<\/th>/);
});

test("right-click places cursor B, measurement table shows signed right-minus-left delta, and A-B zoom works", async () => {
  const { doc, urlState, openTask } = createHarness();
  await openTask();
  const canvas = doc.getElementById("cv-canvas");
  const measurements = doc.getElementById("cv-measurements");
  const rows = doc.getElementById("cv-measure-rows");
  const zoomBtn = doc.getElementById("cv-zoom-cursors");

  assert.equal(measurements.hidden, true);
  assert.equal(zoomBtn.disabled, true);

  // Left-click at x=360 -> t = (360 - 240) / 600 * 10 = 2.0 s sets cursor A
  fire(canvas, "pointerdown", { button: 0, pointerId: 1, clientX: 360, clientY: 80 });
  fire(canvas, "pointerup", { button: 0, pointerId: 1, clientX: 360, clientY: 80 });
  for (let i = 0; i < 4; i++) await Promise.resolve();

  assert.equal(doc.getElementById("cv-cur-a").textContent, "2 s");
  assert.equal(measurements.hidden, false);
  assert.equal(measurements.open, false);
  assert.equal(zoomBtn.disabled, true);

  // Right-click at x=540 -> t = (540 - 240) / 600 * 10 = 5.0 s sets cursor B
  const ctxEv = fire(canvas, "contextmenu", { button: 2, clientX: 540, clientY: 80 });
  for (let i = 0; i < 4; i++) await Promise.resolve();

  assert.equal(ctxEv.defaultPrevented, true);
  assert.equal(doc.getElementById("cv-cur-b").textContent, "5 s");
  assert.equal(doc.getElementById("cv-cur-d").textContent, "3 s");
  assert.equal(measurements.open, true);
  assert.equal(zoomBtn.disabled, false);
  // A=2s -> c1=20, c2=46; B=5s -> c1=50, c2=40; right-left => c1:+30, c2:-6
  assert.match(rows.innerHTML, /MotorSpeed<\/th><td>20<\/td><td>50<\/td><td>30<\/td>/);
  assert.match(rows.innerHTML, /MotorTorque<\/th><td>46<\/td><td>40<\/td><td>-6<\/td>/);

  // Place B to the left of A (t=1.0s, x=300): right(A=2s) - left(B=1s) => c1=20-10=10, c2=46-48=-2
  fire(canvas, "contextmenu", { button: 2, clientX: 300, clientY: 80 });
  for (let i = 0; i < 4; i++) await Promise.resolve();
  assert.match(rows.innerHTML, /MotorSpeed<\/th><td>20<\/td><td>10<\/td><td>10<\/td>/);
  assert.match(rows.innerHTML, /MotorTorque<\/th><td>46<\/td><td>48<\/td><td>-2<\/td>/);

  // Clicking a measurement row toggles lane highlight (.on)
  const fakeRow = { dataset: { laneIndex: "1" }, closest: sel => (sel === "tr[data-lane-index]" ? fakeRow : null) };
  fire(rows, "click", { target: fakeRow });
  assert.match(rows.innerHTML, /data-lane-index="1" class="on"/);

  // Zoom A-B zooms to [1, 2]
  fire(zoomBtn, "click");
  await new Promise(resolve => setTimeout(resolve, 320));
  assert.equal(urlState.cr, "1~2");
});

test("a narrow chart uses the same time coordinates for drawing and cursor input", async () => {
  const { doc, openTask } = createHarness();
  doc.getElementById("cv-wrap").clientWidth = 300;
  await openTask();
  const canvas = doc.getElementById("cv-canvas");
  const midpoint = 206;

  assert.equal(canvas.width, 300);
  const event = fire(canvas, "contextmenu", { button: 2, clientX: midpoint, clientY: 80 });
  assert.equal(event.defaultPrevented, true);
  assert.equal(doc.getElementById("cv-cur-b").textContent, "5 s");
  assert.ok(canvas._ctx2d.calls.some(call => call.op === "fillText" && call.text === "B" && call.x === midpoint + 8));
});

test("cursor line can be dragged directly with live readout and cancel rollback", async () => {
  const { doc, openTask } = createHarness();
  await openTask();
  const canvas = doc.getElementById("cv-canvas");

  // Place cursor A at t = 2s (x = 360)
  fire(canvas, "pointerdown", { button: 0, pointerId: 1, clientX: 360, clientY: 80 });
  fire(canvas, "pointerup", { button: 0, pointerId: 1, clientX: 360, clientY: 80 });
  for (let i = 0; i < 4; i++) await Promise.resolve();

  // Hover near cursor A shows ew-resize cursor
  fire(canvas, "pointermove", { clientX: 362, clientY: 80 });
  assert.equal(canvas.style.cursor, "ew-resize");

  // Drag cursor A toward t = 4s (x = 480) then cancel -> reverts readout to 2 s
  fire(canvas, "pointerdown", { button: 0, pointerId: 9, clientX: 360, clientY: 80 });
  fire(canvas, "pointermove", { button: 0, pointerId: 9, clientX: 480, clientY: 80 });
  assert.equal(doc.getElementById("cv-cur-a").textContent, "4 s");
  fire(canvas, "pointercancel", { button: 0, pointerId: 9, clientX: 480, clientY: 80 });
  assert.equal(doc.getElementById("cv-cur-a").textContent, "2 s");

  // Drag cursor A to t = 5s (x = 540) and commit on pointerup
  fire(canvas, "pointerdown", { button: 0, pointerId: 10, clientX: 360, clientY: 80 });
  fire(canvas, "pointermove", { button: 0, pointerId: 10, clientX: 540, clientY: 80 });
  fire(canvas, "pointerup", { button: 0, pointerId: 10, clientX: 540, clientY: 80 });
  for (let i = 0; i < 4; i++) await Promise.resolve();
  assert.equal(doc.getElementById("cv-cur-a").textContent, "5 s");
});

test("dragging on the top time axis renders zoom band and zooms to dragged span", async () => {
  const { doc, urlState, openTask } = createHarness();
  await openTask();
  const canvas = doc.getElementById("cv-canvas");

  // Hovering time axis shows zoom-in cursor
  fire(canvas, "pointermove", { clientX: 300, clientY: 10 });
  assert.equal(canvas.style.cursor, "zoom-in");

  // Drag on time axis from x=300 (t=1s) to x=480 (t=4s)
  fire(canvas, "pointerdown", { button: 0, pointerId: 3, clientX: 300, clientY: 10 });
  canvas._ctx2d.calls.length = 0;
  fire(canvas, "pointermove", { button: 0, pointerId: 3, clientX: 480, clientY: 10 });
  const bandFill = canvas._ctx2d.calls.find(c => c.op === "fillRect" && c.style === "rgba(94,106,210,.10)");
  assert.ok(bandFill, "time-axis drag renders a zoom selection band");
  assert.equal(bandFill.x, 300);
  assert.equal(bandFill.w, 180);

  fire(canvas, "pointerup", { button: 0, pointerId: 3, clientX: 480, clientY: 10 });
  await new Promise(resolve => setTimeout(resolve, 320));
  assert.equal(urlState.cr, "1~4");
});

test("batch dialog supports fill, wildcard matching, comments, inline errors, and close-before-status", async () => {
  const { doc, openTask } = createHarness();
  await openTask();

  const dialog = doc.getElementById("cv-batch");
  const form = doc.getElementById("cv-batch-form");
  const names = doc.getElementById("cv-batch-names");
  const errEl = doc.getElementById("cv-batch-error");
  const statusEl = doc.getElementById("cv-status");

  // Open dialog and fill current lanes
  fire(doc.getElementById("cv-batch-open"), "click");
  assert.equal(dialog.open, true);
  assert.equal(doc.activeElement, names);

  fire(doc.getElementById("cv-batch-fill"), "click");
  assert.equal(names.value, "m_ACT_MOT_SPD\nm_ACT_MOT_TRQ\nsig_DOOR_SW");

  // Empty input keeps dialog open and reports error inside dialog
  names.value = "  # comment only\n   ";
  const emptyEv = fire(form, "submit", { submitter: { value: "replace" } });
  assert.equal(emptyEv.defaultPrevented, true);
  assert.equal(dialog.open, true);
  assert.match(errEl.textContent, /请输入/);

  // Wildcard + display name + unmatched item: closes dialog BEFORE writing page status
  let dialogOpenDuringStatus = null;
  let statusText = "";
  Object.defineProperty(statusEl, "textContent", {
    get() { return statusText; },
    set(val) { statusText = val; dialogOpenDuringStatus = dialog.open; },
  });

  names.value = "# motor signals\n*DOOR*\nNON_EXISTENT";
  fire(form, "submit", { submitter: { value: "replace" } });
  assert.equal(dialog.open, false);
  assert.equal(dialogOpenDuringStatus, false, "page live region is updated after modal dialog closes");
  assert.match(statusText, /当前显示 1 条泳道/);
  assert.match(statusText, /未匹配：NON_EXISTENT/);

  // Append mode keeps existing lane and adds wildcard matches
  fire(doc.getElementById("cv-batch-open"), "click");
  names.value = "m_ACT_MOT*";
  fire(form, "submit", { submitter: { value: "append" } });
  assert.equal(dialog.open, false);
  assert.match(statusText, /当前显示 3 条泳道/);

  // Cancel submits natively (not prevented)
  fire(doc.getElementById("cv-batch-open"), "click");
  const cancelEv = fire(form, "submit", { submitter: { value: "cancel" } });
  assert.equal(cancelEv.defaultPrevented, false);

  // Switching saved views refreshes cursors cleanly without throwing
  doc.getElementById("cv-view").value = "7";
  fire(doc.getElementById("cv-view"), "change");
  for (let i = 0; i < 4; i++) await Promise.resolve();
  assert.match(doc.getElementById("cv-count").textContent, /泳道 1 \/ 16/);
});
