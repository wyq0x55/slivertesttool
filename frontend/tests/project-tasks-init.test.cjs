const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const scripts = path.resolve(__dirname, "../../app/static/js/lanmatrix");
const template = fs.readFileSync(path.resolve(__dirname,
  "../../app/templates/lanmatrix/project_tasks.html"), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

function harness({ cachedTemplate = false, response } = {}) {
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
    classList: { toggle() {}, add() {}, remove() {} },
    querySelectorAll: () => [],
    addEventListener(event, handler) { this.listeners[event] = handler; },
  }]));
  let calls = 0;
  const context = vm.createContext({
    URLSearchParams, location: { search: "" },
    document: {
      getElementById: (id) => nodes.get(id) || null,
      querySelector: () => ({ dataset: { project: "9" } }),
      querySelectorAll: () => [], addEventListener() {},
    },
    addEventListener() {}, setTimeout() {}, setInterval: () => 1,
    clearInterval() {},
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
  return { get: (id) => nodes.get(id), calls: () => calls };
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
