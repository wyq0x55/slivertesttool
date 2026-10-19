const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const scripts = path.resolve(__dirname, "../../app/static/js/lanmatrix");
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a poll racing a filter request resets the shared pager for the new filter", async () => {
  const nodes = new Map();
  function get(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: "", checked: false, hidden: false, textContent: "", innerHTML: "",
      querySelectorAll: () => [],
    });
    return nodes.get(id);
  }
  let page = 1;
  const pager = {
    reset() { page = 1; }, setTotal() {},
    slice: (rows) => rows.slice((page - 1) * 20, page * 20),
  };
  const requests = [];
  const rows = (status) => Array.from({ length: 61 }, (_, i) => ({
    task_id: `T${i}`, status, project_id: 2, created_at: "2026-10-19T00:00:00Z",
  }));
  const sharedRow = {
    FINAL: ["passed", "failed", "cancelled"],
    cmp: (a, b, key) => String(a[key]).localeCompare(String(b[key])),
    signature: (row) => row.status,
    rowHtml: (row) => `<tr>${row.task_id}</tr>`,
  };
  const window = { LMTaskRow: sharedRow };
  const context = vm.createContext({
    window, document: {
      hidden: false, getElementById: get, querySelector: () => null,
      querySelectorAll: () => [], addEventListener() {},
    },
    LMPill: { TASK_ZH: {}, html: () => "" },
    LMUt: { workspaceTask: (row) => row, ACTIVE: {} },
    LMApi: {
      meTasks() {
        let resolve;
        const promise = new Promise((accept) => { resolve = accept; });
        requests.push({ resolve });
        return promise;
      },
      meOverview: () => new Promise(() => {}),
    },
    setInterval: () => 1, clearInterval() {},
  });
  const source = fs.readFileSync(path.join(scripts, "home.js"), "utf8")
    .replace("global.LMHome = {", "global.LMHome = { loadTasks, pollTasks, renderTasks, setPager: p => taskPager = p,");
  vm.runInContext(source, context);
  const home = window.LMHome;
  home.setPager(pager);
  home.renderTasks({ tasks: rows("running"), projects: [], ut_jobs: [] });
  page = 3;

  get("lm-h-status").value = "failed";
  const filtering = home.loadTasks();
  const polling = home.pollTasks();
  requests[1].resolve({ tasks: rows("failed"), projects: [], ut_jobs: [] });
  await polling;
  requests[0].resolve({ tasks: rows("failed"), projects: [], ut_jobs: [] });
  await filtering;
  await settle();
  assert.equal(page, 1);
  assert.equal((get("lm-h-rows").innerHTML.match(/<tr>/g) || []).length, 20);
});
