const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const scripts = path.resolve(__dirname, "../../app/static/js/lanmatrix");
const template = fs.readFileSync(path.resolve(__dirname,
  "../../app/templates/lanmatrix/project_tasks.html"), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

function harness({ cachedTemplate = false, response, query = {} } = {}) {
  const ids = new Set(Array.from(template.matchAll(/\bid="([^"]+)"/g), (m) => m[1]));
  ids.add("lm-toast");
  if (cachedTemplate) {
    // Markup cached before the whole-project report change.
    ids.delete("lm-project-html-report");
    ids.add("lm-batch-html-report");
    ids.delete("lm-tasks-error");
  }
  const nodes = new Map(Array.from(ids, (id) => [id, {
    value: "", hidden: true, innerHTML: "", textContent: "", listeners: {},
    attributes: {},
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelectorAll: () => [],
    addEventListener(event, handler) { this.listeners[event] = handler; },
  }]));
  const buttons = Array.from(template.matchAll(/<button[^>]*data-status="([^"]*)"[^>]*>/g), (m) => ({
    dataset: { status: m[1] }, listeners: {}, attributes: {},
    classList: { toggle() {} },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(event, handler) { this.listeners[event] = handler; },
  }));
  nodes.get("lm-f-seg").querySelectorAll = () => buttons;
  let calls = 0;
  let poll;
  let urlFilters = { ...query };
  const context = vm.createContext({
    URLSearchParams, location: { search: "" },
    document: {
      getElementById: (id) => nodes.get(id) || null,
      querySelector: (selector) => selector === ".lm-tasks" ? { dataset: { project: "9" } } : null,
      querySelectorAll: () => [], addEventListener() {},
    },
    addEventListener() {}, setTimeout() {}, setInterval(handler) { poll = handler; return 1; },
    clearInterval() {},
    LMUrl: {
      all: () => urlFilters, get: (key) => urlFilters[key], onPop() {},
      replace(values) { urlFilters = { ...values }; },
    },
    LMApi: {
      async listProjectTasks() {
        calls++;
        return response ? response() : {
          tasks: [{ task_id: "T1", test_id: "CASE1", status: "running", progress: 25 }],
          total: 1, models: [],
        };
      },
    },
  });
  context.window = context;
  for (const file of ["pill.js", "task_actions.js", "task_row.js", "project_tasks.js"]) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context,
      { filename: file });
  }
  return {
    get: (id) => nodes.get(id), calls: () => calls,
    filter: (status) => buttons.find((b) => b.dataset.status === status),
    url: () => urlFilters, poll: () => poll(),
  };
}

for (const cachedTemplate of [false, true]) {
  test(`task list initializes with ${cachedTemplate ? "cached" : "current"} markup`, async () => {
    const h = harness({ cachedTemplate });
    await settle();
    assert.equal(h.calls(), 1);
    assert.equal(h.get("lm-tasks-body").hidden, false);
    assert.equal(h.get("lm-tasks-actions").hidden, false);
    assert.match(h.get("lm-task-rows").innerHTML, /CASE1/);
    assert.equal(h.get("lm-toast").textContent, "");
  });
}

test("task load failure stays visible and refresh retries successfully", async () => {
  let fail = true;
  const h = harness({ response() {
    if (fail) throw new Error("server unavailable");
    return { tasks: [], models: [] };
  } });
  await settle();
  assert.equal(h.get("lm-tasks-error").hidden, false);
  assert.match(h.get("lm-tasks-error").textContent, /server unavailable/);
  assert.equal(h.get("lm-tasks-actions").hidden, false);
  fail = false;
  await h.get("lm-refresh").listeners.click();
  assert.equal(h.get("lm-tasks-error").hidden, true);
  assert.equal(h.get("lm-tasks-body").hidden, false);
});

test("permission denial keeps task controls hidden", async () => {
  const h = harness({ response() { throw Object.assign(new Error("forbidden"), { status: 403 }); } });
  await settle();
  assert.equal(h.get("lm-tasks-denied").hidden, false);
  assert.equal(h.get("lm-tasks-body").hidden, true);
  assert.equal(h.get("lm-tasks-actions").hidden, true);
});

const statusTasks = [
  { task_id: "T1", test_id: "QUEUED_CASE", status: "queued" },
  { task_id: "T2", test_id: "RUNNING_CASE", status: "running" },
  { task_id: "T3", test_id: "PASSED_CASE", status: "passed", result: "PASS" },
  { task_id: "T4", test_id: "FAILED_CASE", status: "failed", result: "FAIL" },
  { task_id: "T5", test_id: "ERROR_CASE", status: "failed", result: " error: judge crashed " },
  { task_id: "T6", test_id: "CANCELLED_CASE", status: "cancelled" },
];

test("status buttons filter by displayed verdict and sync the URL", async () => {
  const h = harness({ response: () => ({ tasks: statusTasks }) });
  await settle();
  for (const [status, expected] of [
    ["queued", "QUEUED_CASE"], ["running", "RUNNING_CASE"], ["passed", "PASSED_CASE"],
    ["failed", "FAILED_CASE"], ["error", "ERROR_CASE"], ["cancelled", "CANCELLED_CASE"],
  ]) {
    h.filter(status).listeners.click();
    const html = h.get("lm-task-rows").innerHTML;
    assert.match(html, new RegExp(expected));
    for (const t of statusTasks.filter((t) => t.test_id !== expected)) {
      assert.ok(!html.includes(t.test_id), `${status} excludes ${t.test_id}`);
    }
    assert.equal(h.url().status, status);
    assert.equal(h.filter(status).attributes["aria-pressed"], "true");
    assert.equal(h.filter("").attributes["aria-pressed"], "false");
  }
  h.filter("").listeners.click();
  assert.equal((h.get("lm-task-rows").innerHTML.match(/<tr /g) || []).length, statusTasks.length);
});

for (const status of ["queued", "cancelled", "error"]) {
  test(`restores ${status} filter from URL`, async () => {
    const h = harness({ query: { status }, response: () => ({ tasks: statusTasks }) });
    await settle();
    assert.equal(h.get("lm-f-status").value, status);
    assert.equal(h.filter(status).attributes["aria-pressed"], "true");
    assert.equal((h.get("lm-task-rows").innerHTML.match(/<tr /g) || []).length, 1);
  });
}

test("polling removes tasks leaving a filter and adds tasks entering it", async () => {
  let tasks = statusTasks;
  const h = harness({ query: { status: "queued" }, response: () => ({ tasks }) });
  await settle();
  tasks = statusTasks.map((t) => t.status === "queued" ? { ...t, status: "running" } : t);
  await h.poll();
  assert.equal(h.get("lm-task-rows").innerHTML, "");
  assert.equal(h.get("lm-tasks-empty").hidden, false);
  h.filter("error").listeners.click();
  tasks = tasks.map((t) => t.task_id === "T1" ? { ...t, status: "failed", result: "ERROR: execution" } : t);
  await h.poll();
  assert.match(h.get("lm-task-rows").innerHTML, /QUEUED_CASE/);
  assert.match(h.get("lm-task-rows").innerHTML, /ERROR_CASE/);
});
