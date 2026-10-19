const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const scripts = path.resolve(__dirname, "../../app/static/js/lanmatrix");
const settle = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(capabilities = { edit: true, review: true }, page = "ut_tasks.js") {
  const nodes = new Map();
  function get(id) {
    if (!nodes.has(id)) nodes.set(id, {
      dataset: {}, value: "", textContent: "", innerHTML: "", hidden: false,
      disabled: false, listeners: {}, children: [],
      classList: { toggle() {} },
      setAttribute() {},
      querySelectorAll() { return []; },
      addEventListener(type, listener) { this.listeners[type] = listener; },
      get firstChild() { return this.children[0]; },
      appendChild(node) { this.children.push(node); },
      removeChild(node) { this.children.splice(this.children.indexOf(node), 1); },
    });
    return nodes.get(id);
  }
  const root = get("root");
  root.dataset.project = "4";
  get("ut-detail").hidden = true;
  const details = [], previews = [], reviews = [], revisions = [], batches = [];
  let failList = false, onPop;
  const window = {
    LM: { projectCapabilities: capabilities },
    LMReady: Promise.resolve(),
    scrollTo() {},
  };
  const context = vm.createContext({
    window, document: {
      querySelector(selector) {
        return selector === (page === "ut_pipeline.js" ? ".lm-ut" : ".lm-ut-tasks") ? root : null;
      },
      getElementById: get,
      createElement: () => ({ className: "" }),
      createTextNode: (value) => ({ textContent: value }),
    },
    setTimeout: () => 1, clearTimeout() {},
    LMApi: {
      utJobs: () => failList ? Promise.reject(new Error("list offline")) : Promise.resolve([]),
      utBatches: () => Promise.resolve([]),
      utGetConfig: () => Promise.resolve({ manifest_path: "manifest.json" }),
      utTargets: () => Promise.resolve([
        { id: 1, in_index: true, row_no: 1, function: "f", source_rel: "src/a.c" },
      ]),
      utCreateBatch(...args) {
        const d = deferred(); batches.push({ ...d, args }); return d.promise;
      },
      utJob(pid, id) { const d = deferred(); details.push({ ...d, id }); return d.promise; },
      utAttemptText(pid, job, attempt) {
        const d = deferred(); previews.push({ ...d, job, attempt }); return d.promise;
      },
      utAttemptUrl: (pid, job, attempt) => `/csv/${job}/${attempt}`,
      utReview(...args) { const d = deferred(); reviews.push({ ...d, args }); return d.promise; },
      utRevise(...args) { const d = deferred(); revisions.push({ ...d, args }); return d.promise; },
    },
    LMUI: { alert: async () => {} },
    LMUrl: {
      get: () => "", set() {}, replace() {},
      onPop(listener) { onPop = listener; },
    },
  });
  window.LMUrl = context.LMUrl;
  for (const file of ["pill.js", "ut_common.js", page]) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context);
    context.LMPill = window.LMPill;
    context.LMUt = window.LMUt;
    context.LM = window.LM;
  }
  const click = (id) => get(id).listeners.click.call(get(id));
  function open(id = 1) {
    return get("ut-rows").listeners.click({
      target: { closest: () => ({ dataset: { job: String(id) } }) },
    });
  }
  function pin(attempt) {
    return get("ut-d-attempts").listeners.click({
      target: { closest: () => ({ dataset: { attempt: String(attempt) } }) },
    });
  }
  return { get, root, details, previews, reviews, revisions, batches, open, pin, click,
    pop: (q) => onPop(q), failList: () => { failList = true; }, common: window.LMUt };
}

function candidate(id, seq) {
  return { id, seq, origin: "GENERATOR", has_csv: true, verdict: "PASS",
    check: { issues: [] }, created_at: "2026-10-19T12:00:00Z" };
}
function job(current = 10, state = "awaiting_review") {
  return { id: 1, batch_id: 1, state, current_attempt_id: current,
    repair_rounds: 0, max_repair_rounds: 5, last_verdict: "PASS",
    target: { function: "f", source_rel: "src/a.c" },
    attempts: [candidate(10, 1), ...(current === 20 ? [candidate(20, 2)] : [])] };
}
async function ready(h, value = job()) {
  await settle();
  h.open();
  h.details.at(-1).resolve(value);
  await settle();
}

test("Back during the initial detail request invalidates the late response", async () => {
  const h = harness();
  await settle();
  h.open();
  assert.equal(h.root.hidden, true);
  h.pop({});
  h.details[0].resolve(job());
  await settle();
  assert.equal(h.root.hidden, false);
  assert.equal(h.get("ut-detail").hidden, true);
});

test("same-job detail responses cannot restore an older candidate", async () => {
  const h = harness();
  await ready(h);
  h.click("ut-d-refresh");
  await settle();
  const older = h.details.at(-1);
  h.click("ut-d-refresh");
  await settle();
  const newer = h.details.at(-1);
  newer.resolve(job(20));
  await settle();
  older.resolve(job(10));
  await settle();
  assert.equal(h.get("ut-d-download").href, "/csv/1/20");
});

test("automatic preview follows the latest candidate and ignores late CSV text", async () => {
  const h = harness();
  await ready(h);
  const old = h.previews.at(-1);
  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve(job(20));
  await settle();
  h.previews.at(-1).resolve({ text: "new candidate" });
  await settle();
  old.resolve({ text: "old candidate" });
  await settle();
  assert.match(h.get("ut-d-csv-title").textContent, /第 2 版/);
  assert.equal(h.get("ut-d-csv").textContent, "new candidate");
});

test("historical preview is explicit and cannot approve the unseen current candidate", async () => {
  const h = harness();
  await ready(h, job(20));
  h.pin(10);
  assert.match(h.get("ut-d-csv-title").textContent, /历史版本/);
  assert.equal(h.get("ut-d-approve").disabled, true);
  h.pin(20);
  assert.equal(h.get("ut-d-approve").disabled, false);
});

test("successful revision followed by a failed refresh keeps review disabled", async () => {
  const h = harness();
  await ready(h);
  h.click("ut-d-refresh");
  await settle();
  const stale = h.details.at(-1);
  h.get("ut-d-revision").value = "new CSV";
  h.click("ut-d-revise");
  h.failList();
  h.revisions[0].resolve({});
  await settle();
  stale.resolve(job());
  await settle();
  assert.equal(h.get("ut-d-gate-forms").hidden, true);
  assert.equal(h.get("ut-d-approve").disabled, true);
  assert.match(h.get("ut-detail-status").textContent, /offline/);
});

test("a page without a candidate never offers approval", async () => {
  const h = harness();
  const empty = job(null, "needs_human");
  empty.attempts = [];
  await ready(h, empty);
  assert.equal(h.get("ut-d-approve").disabled, true);
  h.click("ut-d-approve");
  assert.equal(h.reviews.length, 0);
});

test("duplicate review clicks send only one request, pinned to the displayed candidate", async () => {
  const h = harness();
  await ready(h);
  h.click("ut-d-approve");
  h.click("ut-d-approve");
  assert.equal(h.reviews.length, 1);
  assert.equal(h.reviews[0].args[4], 10);
});

test("a reader can inspect evidence but cannot submit review or revision", async () => {
  const h = harness({ edit: false, review: false });
  await ready(h);
  assert.equal(h.get("ut-d-gate-forms").hidden, true);
  h.click("ut-d-approve");
  h.get("ut-d-revision").value = "CSV";
  h.click("ut-d-revise");
  assert.equal(h.reviews.length, 0);
  assert.equal(h.revisions.length, 0);
});

test("dashboard keeps machine PASS separate from human approval and excludes removed functions", () => {
  const h = harness();
  const snapshot = h.common.dashboardSnapshot([
    { in_index: true, latest_job: { state: "approved", last_verdict: "FAIL" } },
    { in_index: true, latest_job: { state: "awaiting_review", last_verdict: "PASS" } },
    { in_index: true, latest_job: null },
    { in_index: false, latest_job: { state: "approved", last_verdict: "PASS" } },
  ]);
  assert.equal(snapshot.summary.total, 3);
  assert.equal(snapshot.summary.passed, 1);
  assert.equal(snapshot.review.approved, 1);
  assert.equal(snapshot.review.pending, 1);
  assert.equal(snapshot.summary.not_run, 1);
});

test("repeated enqueue clicks submit one batch and keep the selected function ids", async () => {
  const h = harness({ edit: true }, "ut_pipeline.js");
  await settle();
  h.get("ut-rows").listeners.change({ target: { dataset: { target: "1" }, checked: true } });
  h.click("ut-enqueue");
  h.click("ut-enqueue");
  assert.equal(h.batches.length, 1);
  assert.deepEqual(Array.from(h.batches[0].args[1]), [1]);
  assert.equal(h.get("ut-enqueue").disabled, true);
  h.batches[0].resolve({ id: 7 });
  await settle();
  assert.equal(h.get("ut-enqueue").disabled, true);
});

test("UT jobs use the shared task row with separate result and review cells", () => {
  const window = {};
  const context = vm.createContext({
    window, document: {},
    LMApi: { projectTaskDownloadUrl() { throw new Error("UT must not call Silver APIs"); } },
  });
  for (const file of ["pill.js", "ut_common.js", "task_row.js"]) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context);
    context.LMPill = window.LMPill;
  }
  const raw = { ...job(), state: "approved", last_verdict: "FAIL",
    project_id: 4, updated_at: "2026-10-19T12:00:00Z", task_status: "failed" };
  const task = window.LMUt.workspaceTask(raw);
  assert.equal(task.task_id, "ut:1");
  const html = window.LMTaskRow.rowHtml(task, {
    projectId: 4, canDelete: true, viewHref: "/lanmatrix/projects/4/tasks?job=1",
    projectCell: "<td>Project</td>",
  });
  assert.equal((html.match(/<tr /g) || []).length, 1);
  assert.equal((html.match(/<td[ >]/g) || []).length, 10);
  assert.match(html, /lm-cell-status[\s\S]*?FAIL/);
  assert.match(html, /lm-cell-review[\s\S]*?已通过/);
  assert.doesNotMatch(html, /lm-task-(sel|steps|cancel|retest|del)/);
  assert.match(html, /WinAMS/);
  const changed = window.LMUt.workspaceTask({ ...raw, current_attempt_id: 20 });
  assert.notEqual(window.LMTaskRow.signature(task), window.LMTaskRow.signature(changed));
});
