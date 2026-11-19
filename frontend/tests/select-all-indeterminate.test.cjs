const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const scripts = path.resolve(__dirname, "../../app/static/js/lanmatrix");
const templates = path.resolve(__dirname, "../../app/templates/lanmatrix");
const settle = async (rounds = 8) => {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
};

// Minimal element: every property the two list pages touch while painting
// rows, plus a listener registry the tests can fire by hand.
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
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
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

// The row checkboxes are only ever produced as HTML text, so rebuild fake
// elements from the latest innerHTML — one cache per rendered string keeps the
// listeners bound by bindRowActions attached to the elements the test fires.
function selectionBoxes(html) {
  const boxes = [];
  for (const match of String(html).matchAll(/<input[^>]*class="lm-task-sel"[^>]*>/g)) {
    const tag = match[0];
    const key = /data-k="([^"]*)"/.exec(tag);
    const box = makeNode("sel:" + (key ? key[1] : ""));
    box.dataset = { k: key ? key[1] : "" };
    box.checked = /\schecked[\s>]/.test(tag);
    boxes.push(box);
  }
  return boxes;
}

function asRowHost(node) {
  let cache = { html: null, boxes: [] };
  node.querySelectorAll = function (selector) {
    if (selector !== ".lm-task-sel") return [];
    if (cache.html !== this.innerHTML) {
      cache = { html: this.innerHTML, boxes: selectionBoxes(this.innerHTML) };
    }
    return cache.boxes;
  };
  return node;
}

function loadTemplatesNodes(file) {
  const template = fs.readFileSync(path.join(templates, file), "utf8");
  const ids = new Set(Array.from(template.matchAll(/\bid="([^"]+)"/g), (m) => m[1]));
  ids.add("lm-toast");
  return new Map(Array.from(ids, (id) => [id, makeNode(id)]));
}

function projectTasksHarness(tasks) {
  const nodes = loadTemplatesNodes("project_tasks.html");
  const rows = asRowHost(nodes.get("lm-task-rows"));
  const context = vm.createContext({
    URLSearchParams,
    location: { search: "" },
    document: {
      getElementById: (id) => nodes.get(id) || null,
      querySelector: (selector) => (selector === ".lm-tasks" ? { dataset: { project: "9" } } : null),
      querySelectorAll: (selector) => (
        selector === ".lm-task-sel" ? rows.querySelectorAll(selector) : []
      ),
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
  return { get: (id) => nodes.get(id), rows };
}

function homeHarness(tasks) {
  const nodes = loadTemplatesNodes("home.html");
  const rows = asRowHost(nodes.get("lm-h-rows"));
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
      querySelectorAll: () => [],
      addEventListener(event, handler) {
        (documentListeners[event] || (documentListeners[event] = [])).push(handler);
      },
    },
    addEventListener() {},
    setTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
    LMApi: {
      async meOverview() { return { kpi: {}, projects: [project] }; },
      async meTasks() { return { tasks, projects: [project], ut_jobs: [] }; },
    },
  });
  context.window = context;
  for (const file of ["pill.js", "task_row.js", "home.js"]) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context,
      { filename: file });
  }
  return {
    get: (id) => nodes.get(id),
    rows,
    start() { (documentListeners.DOMContentLoaded || []).forEach((fn) => fn()); },
  };
}

const tasks = () => [
  { task_id: "T1", test_id: "C1", status: "passed", progress: 100 },
  { task_id: "T2", test_id: "C2", status: "passed", progress: 100 },
  { task_id: "T3", test_id: "C3", status: "passed", progress: 100 },
];

test("project task list: empty, partial and full selection drive both states", async () => {
  const empty = projectTasksHarness([]);
  // Deliberately dirty the DOM first: the empty snapshot must reset both,
  // proving .indeterminate is written rather than merely left alone.
  empty.get("lm-check-all").checked = true;
  empty.get("lm-check-all").indeterminate = true;
  await settle();
  assert.equal(empty.get("lm-check-all").checked, false);
  assert.equal(empty.get("lm-check-all").indeterminate, false);

  const h = projectTasksHarness(tasks());
  await settle();
  const all = h.get("lm-check-all");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, false);

  // all of N
  all.checked = true;
  all.fire("change");
  assert.equal(all.checked, true);
  assert.equal(all.indeterminate, false);

  // some of N: one row unticked by hand
  let boxes = h.rows.querySelectorAll(".lm-task-sel");
  assert.equal(boxes.length, 3);
  const t2 = boxes.find((box) => box.dataset.k === "T2");
  t2.checked = false;
  t2.fire("change");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, true);

  // partial, then tick the rest by hand: the mixed state must clear
  t2.checked = true;
  t2.fire("change");
  assert.equal(all.checked, true);
  assert.equal(all.indeterminate, false);

  // partial, then select-all: the mixed state must clear there too
  boxes = h.rows.querySelectorAll(".lm-task-sel");
  boxes[0].checked = false;
  boxes[0].fire("change");
  boxes[1].checked = false;
  boxes[1].fire("change");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, true);
  all.checked = true;
  all.fire("change");
  assert.equal(all.checked, true);
  assert.equal(all.indeterminate, false);

  // select-none still clears everything
  all.checked = false;
  all.fire("change");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, false);
});

test("workspace list: empty, partial and full selection drive both states", async () => {
  const empty = homeHarness([]);
  empty.get("lm-h-check-all").checked = true;
  empty.get("lm-h-check-all").indeterminate = true;
  empty.start();
  await settle();
  assert.equal(empty.get("lm-h-check-all").checked, false);
  assert.equal(empty.get("lm-h-check-all").indeterminate, false);

  const h = homeHarness(tasks());
  h.start();
  await settle();
  const all = h.get("lm-h-check-all");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, false);

  all.checked = true;
  all.fire("change");
  assert.equal(all.checked, true);
  assert.equal(all.indeterminate, false);

  let boxes = h.rows.querySelectorAll(".lm-task-sel");
  assert.equal(boxes.length, 3);
  const t1 = boxes.find((box) => box.dataset.k === "T1");
  t1.checked = false;
  t1.fire("change");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, true);

  t1.checked = true;
  t1.fire("change");
  assert.equal(all.checked, true);
  assert.equal(all.indeterminate, false);

  boxes = h.rows.querySelectorAll(".lm-task-sel");
  boxes[0].checked = false;
  boxes[0].fire("change");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, true);
  all.checked = true;
  all.fire("change");
  assert.equal(all.checked, true);
  assert.equal(all.indeterminate, false);

  all.checked = false;
  all.fire("change");
  assert.equal(all.checked, false);
  assert.equal(all.indeterminate, false);
});