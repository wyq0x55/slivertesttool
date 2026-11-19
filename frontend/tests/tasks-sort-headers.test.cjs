const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const scripts = path.join(root, "app/static/js/lanmatrix");
const template = fs.readFileSync(
  path.join(root, "app/templates/lanmatrix/project_tasks.html"), "utf8");

const SORT_TH = ".lm-tasks-table thead th[data-sort]";
const SORT_BTN = SORT_TH + " .lm-sort-btn";
const settle = async (rounds = 16) => {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
};

const LABELS = {
  test_id: "test id",
  sil_name: "模型",
  submitter: "提交者",
  status: "状态 / 结果",
  review: "审核",
  progress: "进度",
  finished_at: "完成时间",
};

// --- template -------------------------------------------------------------
// Derived from the real markup, so a header that loses its button fails here
// instead of silently regressing the table.
const taskHeaders = [...template.matchAll(/<th\b([^>]*)>([\s\S]*?)<\/th>/g)]
  .map(([, attrs, inner]) => ({ attrs, inner }))
  .filter(({ attrs }) => /\bdata-sort="/.test(attrs));

const headerKey = (markup) => /data-sort="([^"]+)"/.exec(markup)[1];

test("every sortable task header is a scope cell wrapping a native button", () => {
  assert.ok(taskHeaders.length >= 7, "all sortable columns are present");
  for (const { attrs, inner } of taskHeaders) {
    const key = headerKey(attrs);
    assert.match(attrs, /\bscope="col"/, `${key}: th declares its scope`);
    const button = /<button\b([^>]*)>([\s\S]*?)<\/button>/.exec(inner);
    assert.ok(button, `${key}: header holds a button, not bare clickable text`);
    assert.match(button[1], /\btype="button"/, `${key}: native button type`);
    assert.match(button[1], new RegExp(`\\bclass="lm-sort-btn"`), `${key}: sort-button class`);
    assert.match(button[1], new RegExp(`data-sort="${key}"`), `${key}: key on button`);
    assert.equal(button[2].trim(), LABELS[key], `${key}: visible text unchanged`);
  }

  // The attribute is a live state, never baked into the server markup, and
  // "none" is not a state any inactive header may carry.
  assert.doesNotMatch(template, /aria-sort=/);

  // The checkbox column is furniture, not a column header: untouched.
  const check = /<th\b([^>]*)>\s*<input[^>]*id="lm-check-all"/.exec(template);
  assert.ok(check, "checkbox header still present");
  assert.doesNotMatch(check[1], /data-sort=/, "checkbox header stays unsortable");
  assert.doesNotMatch(check[1], /lm-sort-btn|lm-sortable/, "checkbox header has no sort button");
});

// --- DOM shim: only what project_tasks.js touches --------------------------
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
    scrollIntoView() {},
    replaceChildren() { this.innerHTML = ""; },
    addEventListener(event, handler) {
      (this.listeners[event] || (this.listeners[event] = [])).push(handler);
    },
    fire(event) {
      const payload = { preventDefault() {} };
      (this.listeners[event] || []).forEach((handler) => handler(payload));
    },
    querySelector: () => null,
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
    focusCalls: 0,
    listeners: {},
    getAttribute(name) { return attributes[name] ?? null; },
    setAttribute(name, value) { attributes[name] = String(value); },
    addEventListener(event, handler) {
      (this.listeners[event] || (this.listeners[event] = [])).push(handler);
    },
    fire(event = "click") {
      (this.listeners[event] || []).forEach((handler) => handler({ preventDefault() {} }));
    },
    focus() { this.focusCalls += 1; },
  };
}

// A <th> as project_tasks.js sees it: data-sort + aria-sort + the button inside.
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
      toggle(name, on) {
        if (on === undefined ? !classes.has(name) : on) classes.add(name);
        else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
    querySelector(selector) { return selector === ".lm-sort-btn" ? button : null; },
    textContent: "",
  };
}

const tasks = [
  { task_id: "T1", test_id: "B_CASE", status: "passed", result: "PASS",
    review: { status: "approved" } },
  { task_id: "T2", test_id: "A_CASE", status: "running",
    review: { status: "pending" } },
  { task_id: "T3", test_id: "C_CASE", status: "failed", result: "FAIL" },
];

function taskHarness() {
  const nodes = loadTemplatesNodes();
  const headers = taskHeaders.map(({ attrs }) => makeSortHeader(headerKey(attrs)));
  const context = vm.createContext({
    URLSearchParams,
    location: { search: "" },
    document: {
      hidden: false,
      getElementById: (id) => nodes.get(id) || null,
      querySelector: (selector) => (
        selector === ".lm-tasks" ? { dataset: { project: "9" } } : null
      ),
      querySelectorAll: (selector) => {
        if (selector === SORT_TH) return headers;
        if (selector === SORT_BTN) return headers.map((header) => header.button);
        return [];
      },
      addEventListener() {},
    },
    addEventListener() {},
    setTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
    LMUrl: { all: () => ({}), get: () => "", onPop() {}, replace() {} },
    LMApi: {
      async listProjectTasks() {
        return { tasks, total: tasks.length, models: [] };
      },
    },
  });
  context.window = context;
  for (const file of ["pill.js", "task_actions.js", "task_row.js", "project_tasks.js"]) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context,
      { filename: file });
  }
  return {
    nodes,
    headers,
    header: (key) => headers.find((header) => header.key === key),
    ariaSort: (key) => headers.find((header) => header.key === key).getAttribute("aria-sort"),
    // Paint order of the task keys, read back from the rendered <tbody>. The
    // key appears several times per row; its first occurrence is the row.
    rowOrder() {
      const html = nodes.get("lm-task-rows").innerHTML;
      return tasks.map((t) => t.task_id)
        .sort((a, b) => html.indexOf(`data-k="${a}"`) - html.indexOf(`data-k="${b}"`));
    },
  };
}

test("clicking a header sorts by that key and keeps aria-sort on one th", async () => {
  const h = taskHarness();
  await settle();

  // First render: the default key is task_id, which has no column, so the
  // table is in natural order and no header claims to be sorted.
  for (const header of h.headers) {
    assert.equal(header.getAttribute("aria-sort"), null,
      `${header.key}: no aria-sort before a column is chosen`);
  }
  assert.deepEqual(h.rowOrder(), ["T3", "T2", "T1"]);

  // First click: ascending by test_id -> A_CASE (T2), B_CASE (T1), C_CASE (T3).
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

  // No header ever announces "none": an inactive column is attribute-free.
  for (const header of h.headers) {
    assert.notEqual(header.getAttribute("aria-sort"), "none", `${header.key}: never "none"`);
  }

  // The sort never moves focus itself: the thead is static, so the button the
  // keyboard user activated survives the row repaint.
  for (const header of h.headers) {
    assert.equal(header.button.focusCalls, 0, `${header.key}: sort does not call focus()`);
  }
});

test("a review-first column starts descending, matching the list rule", async () => {
  const h = taskHarness();
  await settle();

  // review must start descending (待审核 before 已通过): pending T2 (rank 3),
  // approved T1 (rank 1), no review T3 (rank 0).
  h.header("review").button.fire("click");
  await settle();
  assert.equal(h.ariaSort("review"), "descending");
  assert.deepEqual(h.rowOrder(), ["T2", "T1", "T3"]);

  // Clicking it again flips to ascending, the lowest rank first.
  h.header("review").button.fire("click");
  await settle();
  assert.equal(h.ariaSort("review"), "ascending");
  assert.deepEqual(h.rowOrder(), ["T3", "T1", "T2"]);
});