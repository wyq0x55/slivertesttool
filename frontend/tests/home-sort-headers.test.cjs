const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const scripts = path.join(root, "app/static/js/lanmatrix");
const template = fs.readFileSync(path.join(root, "app/templates/lanmatrix/home.html"), "utf8");

const SORT_TH = "#lm-pane-tasks thead th[data-sort]";
const SORT_BTN = SORT_TH + " .lm-sort-btn";
const settle = async (rounds = 16) => {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
};

// --- template -------------------------------------------------------------
// Derived from the real markup, so a header that loses its button fails here
// instead of silently regressing the table.
const taskHeaders = [...template.matchAll(/<th\b([^>]*)>([\s\S]*?)<\/th>/g)]
  .map(([, attrs, inner]) => ({ attrs, inner }))
  .filter(({ attrs }) => /\bdata-sort="/.test(attrs));

const headerKey = (markup) => /data-sort="([^"]+)"/.exec(markup)[1];

const LABELS = {
  project: "项目",
  test_id: "test id / 函数",
  sil_name: "模型 / 执行器",
  submitter: "提交者",
  status: "状态 / 结果",
  review: "审核",
  progress: "进度 / 修正",
  finished_at: "完成 / 更新时间",
};

// --- DOM shim: only what home.js actually touches --------------------------
function makeNode(id) {
  return {
    id,
    value: "",
    checked: false,
    indeterminate: false,
    hidden: true,
    innerHTML: "",
    textContent: "",
    tabIndex: 0,
    dataset: {},
    options: [],
    attributes: {},
    listeners: {},
    classList: {
      toggle() {},
      add() {},
      remove() {},
      contains() { return false; },
    },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    getAttribute(name) { return this.attributes[name] ?? null; },
    contains() { return false; },
    closest() { return null; },
    focus() {},
    replaceChildren() { this.innerHTML = ""; },
    addEventListener(event, handler) {
      (this.listeners[event] || (this.listeners[event] = [])).push(handler);
    },
    fire(event) {
      const payload = { preventDefault() {} };
      (this.listeners[event] || []).forEach((handler) => handler(payload));
    },
    querySelectorAll() { return []; },
  };
}

function loadTemplatesNodes() {
  const ids = new Set(Array.from(template.matchAll(/\bid="([^"]+)"/g), (m) => m[1]));
  ids.add("lm-toast");
  return new Map(Array.from(ids, (id) => [id, makeNode(id)]));
}

function makeSortButton(key) {
  const attributes = { class: "lm-sort-btn", "data-sort": key };
  return {
    textContent: LABELS[key],
    listeners: {},
    getAttribute(name) { return attributes[name] ?? null; },
    setAttribute(name, value) { attributes[name] = String(value); },
    addEventListener(event, handler) {
      (this.listeners[event] || (this.listeners[event] = [])).push(handler);
    },
    fire(event = "click") {
      (this.listeners[event] || []).forEach((handler) => handler({ preventDefault() {} }));
    },
  };
}

// A <th> as home.js sees it: data-sort + aria-sort + the button inside it.
function makeSortHeader(key) {
  const attributes = { class: "lm-sortable", "data-sort": key };
  const classes = new Set(["lm-sortable"]);
  const button = makeSortButton(key);
  return {
    key,
    button,
    getAttribute(name) { return attributes[name] ?? null; },
    setAttribute(name, value) { attributes[name] = String(value); },
    removeAttribute(name) { delete attributes[name]; },
    classList: {
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      contains: (name) => classes.has(name),
    },
    querySelector: (selector) => (selector === ".lm-sort-btn" ? button : null),
  };
}

const TASKS = [
  // created_at ascending order is T1, T2, T3; the default sort (task_id,
  // newest first) therefore paints T3, T2, T1.
  { task_id: "T1", test_id: "C2", status: "passed", progress: 100,
    created_at: "2026-01-01T00:00:00Z", finished_at: "2026-01-01T00:00:00Z",
    submitter: "u1", sil_name: "m1", review: { status: "approved" } },
  { task_id: "T2", test_id: "C1", status: "running", progress: 40,
    created_at: "2026-01-02T00:00:00Z", finished_at: "2026-01-02T00:00:00Z",
    submitter: "u2", sil_name: "m2", review: { status: "pending" } },
  { task_id: "T3", test_id: "C3", status: "failed", progress: 100,
    created_at: "2026-01-03T00:00:00Z", finished_at: "2026-01-03T00:00:00Z",
    submitter: "u3", sil_name: "m3", review: { status: "rejected" } },
];

function homeHarness(tasks = TASKS) {
  const nodes = loadTemplatesNodes();
  const headers = taskHeaders.map(({ attrs }) => makeSortHeader(headerKey(attrs)));
  const documentListeners = {};
  const project = { id: 9, code: "P9", name: "Proj 9", can_delete: false };
  const context = vm.createContext({
    URLSearchParams,
    location: { search: "" },
    document: {
      hidden: false,
      activeElement: null,
      getElementById: (id) => nodes.get(id) || null,
      querySelector: () => null,
      querySelectorAll: (selector) => {
        if (selector === SORT_TH) return headers;
        if (selector === SORT_BTN) return headers.map((header) => header.button);
        return [];
      },
      addEventListener(event, handler) {
        (documentListeners[event] || (documentListeners[event] = [])).push(handler);
      },
    },
    addEventListener() {},
    setTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
    LMUrl: { all: () => ({}), get: () => "", onPop() {}, replace() {} },
    LMApi: {
      async meOverview() { return { kpi: {}, projects: [project] }; },
      async meTasks() { return { tasks, projects: [project], ut_jobs: [] }; },
      projectTaskDownloadUrl: (pid, tid) => `/lanmatrix/projects/${pid}/tasks/${tid}/download`,
    },
  });
  context.window = context;
  for (const file of ["pill.js", "task_row.js", "home.js"]) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context,
      { filename: file });
  }
  return {
    nodes,
    headers,
    start() { (documentListeners.DOMContentLoaded || []).forEach((fn) => fn()); },
    header: (key) => headers.find((header) => header.key === key),
    ariaSort: (key) => headers.find((header) => header.key === key).getAttribute("aria-sort"),
    // Paint order of the task keys, read back from the rendered <tbody>.
    rowOrder() {
      const html = nodes.get("lm-h-rows").innerHTML;
      return tasks.map((t) => t.task_id)
        .sort((a, b) => html.indexOf(`data-k="${a}"`) - html.indexOf(`data-k="${b}"`));
    },
  };
}

// --- tests ----------------------------------------------------------------
test("template: every sortable task header is a scoped native button", () => {
  assert.equal(taskHeaders.length, 8);
  assert.deepEqual(taskHeaders.map(({ attrs }) => headerKey(attrs)),
    Object.keys(LABELS));

  for (const { attrs, inner } of taskHeaders) {
    const key = headerKey(attrs);
    assert.match(attrs, /\bscope="col"/, `${key}: th needs scope="col"`);
    assert.match(attrs, /\bclass="lm-sortable"/, `${key}: caret/styling class kept`);
    const button = /<button\b([^>]*)>([\s\S]*?)<\/button>/.exec(inner);
    assert.ok(button, `${key}: header must contain a button`);
    assert.match(button[1], /\btype="button"/, `${key}: button must be type=button`);
    assert.match(button[1], /\bclass="lm-sort-btn"/, `${key}: house class`);
    assert.match(button[1], new RegExp(`data-sort="${key}"`), `${key}: key on button`);
    assert.equal(button[2].trim(), LABELS[key], `${key}: visible text unchanged`);
  }

  // The attribute is a live state, never baked into the server markup, and
  // "none" is not a state any inactive header may carry.
  assert.doesNotMatch(template, /aria-sort=/);
});

test("clicking a header sorts by that key and keeps aria-sort on one th", async () => {
  const h = homeHarness();
  h.start();
  await settle();

  // First render: the default key is task_id, which has no column, so the
  // table is in natural order and no header claims to be sorted.
  for (const header of h.headers) {
    assert.equal(header.getAttribute("aria-sort"), null,
      `${header.key}: no aria-sort before a column is chosen`);
  }
  assert.deepEqual(h.rowOrder(), ["T3", "T2", "T1"]);

  // First click: ascending by test_id -> C1 (T2), C2 (T1), C3 (T3).
  h.header("test_id").button.fire("click");
  await settle();
  assert.deepEqual(h.rowOrder(), ["T2", "T1", "T3"]);
  assert.equal(h.ariaSort("test_id"), "ascending");
  for (const header of h.headers.filter((item) => item.key !== "test_id")) {
    assert.equal(header.getAttribute("aria-sort"), null,
      `${header.key}: inactive header must carry no aria-sort`);
  }

  // Second click: the same header flips direction and its announced state.
  h.header("test_id").button.fire("click");
  await settle();
  assert.deepEqual(h.rowOrder(), ["T3", "T1", "T2"]);
  assert.equal(h.ariaSort("test_id"), "descending");
  assert.equal(h.headers.filter((item) => item.getAttribute("aria-sort") !== null).length, 1);

  // Moving to another column moves the state with it.
  h.header("status").button.fire("click");
  await settle();
  // status ascending: failed (T3), passed (T1), running (T2).
  assert.deepEqual(h.rowOrder(), ["T3", "T1", "T2"]);
  assert.equal(h.ariaSort("status"), "ascending");
  assert.equal(h.ariaSort("test_id"), null);

  const states = h.headers.map((header) => header.getAttribute("aria-sort"));
  assert.equal(states.filter((state) => state !== null).length, 1);
  assert.ok(states.every((state) => state === null || state === "ascending" || state === "descending"));
  assert.ok(!states.includes("none"));
});

test("first render clears stale sort state instead of announcing a phantom column", async () => {
  const h = homeHarness();
  // Simulate a bfcache/restored markup that carries an old annotation.
  h.header("project").setAttribute("aria-sort", "ascending");
  h.start();
  await settle();

  assert.equal(h.header("project").getAttribute("aria-sort"), null);
  assert.ok(h.headers.every((header) => header.getAttribute("aria-sort") === null));
});