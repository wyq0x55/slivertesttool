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

function harness(capabilities = { edit: true, review: true }, page = "ut_tasks.js", lists = {}) {
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
  const details = [], previews = [], reviews = [], revisions = [], batches = [], sheets = [], downloads = [];
  const stops = [], diagnostics = [], coverages = [];
  let failList = false, onPop;
  // Browsers always provide visibility events; timers are recorded so a test
  // can fire the next poll itself.
  const docListeners = {}, timers = [], calls = { jobs: 0, targets: 0 };
  const document = {
    hidden: false,
    addEventListener(type, listener) { docListeners[type] = listener; },
    querySelector(selector) {
      return selector === (page === "ut_pipeline.js" ? ".lm-ut" : ".lm-ut-tasks") ? root : null;
    },
    getElementById: get,
    createElement: () => ({ className: "", click() { downloads.push({ href: this.href, download: this.download }); }, remove() {} }),
    body: { appendChild() {} },
    createTextNode: (value) => ({ textContent: value }),
  };
  const window = {
    LM: { projectCapabilities: capabilities },
    LMReady: Promise.resolve(),
    scrollTo() {},
    Blob,
    URL: { createObjectURL(blob) { downloads.push(blob); return "blob:diagnostic"; }, revokeObjectURL() {} },
    setTimeout: (callback) => timers.push(callback),
    LMStepsEditor: { open(item, opts) { sheets.push({ item, opts }); }, close() {} },
  };
  const context = vm.createContext({
    window, document,
    setTimeout: (callback) => timers.push(callback), clearTimeout() {},
    LMApi: {
      utJobs: () => {
        calls.jobs += 1;
        return failList ? Promise.reject(new Error("list offline")) : Promise.resolve(lists.jobs || []);
      },
      utBatches: () => Promise.resolve([]),
      utGetConfig: () => Promise.resolve({ manifest_path: "manifest.json" }),
      utTargets: () => {
        calls.targets += 1;
        return Promise.resolve(lists.targets || [
          { id: 1, in_index: true, row_no: 1, function: "f", source_rel: "src/a.c" },
        ]);
      },
      utCreateBatch(...args) {
        const d = deferred(); batches.push({ ...d, args }); return d.promise;
      },
      utJob(pid, id) { const d = deferred(); details.push({ ...d, id }); return d.promise; },
      utAttemptText(pid, job, attempt) {
        const d = deferred(); previews.push({ ...d, job, attempt }); return d.promise;
      },
      utAttemptDiagnostic(pid, job, attempt) {
        const d = deferred(); diagnostics.push({ ...d, job, attempt }); return d.promise;
      },
      utAttemptCoverage(pid, job, attempt) {
        const d = deferred(); coverages.push({ ...d, job, attempt }); return d.promise;
      },
      utAttemptUrl: (pid, job, attempt) => `/csv/${job}/${attempt}`,
      utArtifactUrl: (pid, job, attempt, artifact) => `/artifact/${job}/${attempt}/${artifact}`,
      utExportUrl: (pid, batch) => `/export/${batch || "all"}`,
      utReview(...args) { const d = deferred(); reviews.push({ ...d, args }); return d.promise; },
      utRevise(...args) { const d = deferred(); revisions.push({ ...d, args }); return d.promise; },
      utStop(pid, id) { const d = deferred(); stops.push({ ...d, id }); return d.promise; },
    },
    LMUI: { alert: async () => {}, confirm: async (options) => lists.confirm ? lists.confirm(options) : true },
    LMUrl: {
      get: () => "", set() {}, replace() {},
      onPop(listener) { onPop = listener; },
    },
  });
  window.LMUrl = context.LMUrl;
  for (const file of ["pill.js", "ut_common.js", "ut_testcsv.js", page]) {
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
  function pin(attempt, act) {
    return get("ut-d-attempts").listeners.click({
      target: { closest: () => ({ dataset: { attempt: String(attempt), ...(act ? { act } : {}) } }) },
    });
  }
  return { get, root, details, previews, reviews, revisions, batches, sheets, stops, open, pin, click,
    pop: (q) => onPop(q), failList: () => { failList = true; }, common: window.LMUt,
    document, docListeners, timers, calls, downloads, diagnostics, coverages };
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
function issueTab(h, code, version) {
  h.get("ut-d-issues").listeners.click({ target: { closest(selector) {
    return selector === "[data-issue-tab]" ? { dataset: { issueTab: code, issueVersion: String(version) } } : null;
  } } });
  return h.get("ut-d-issues").innerHTML;
}
// Issues are shown one type at a time; this reads every tab in order.
function allIssues(h, version = 10) {
  const first = h.get("ut-d-issues").innerHTML;
  const codes = [...first.matchAll(/data-issue-tab="([^"]+)"/g)].map((m) => m[1]);
  return codes.length ? codes.map((code) => issueTab(h, code, version)).join("") : first;
}

test("step timing shows frozen, running, failed and per-version records safely", async () => {
  const h = harness(), value = job(20, "executing");
  value.timing = { history_complete: true, steps: [
    { step: "queue", kind: "wait", status: "completed", duration_ms: 0 },
    { step: "host_execute", status: "completed", duration_ms: 1250, attempt_seq: 1 },
    { step: "input_model", status: "failed", duration_ms: 2500, attempt_seq: 2, round: 1 },
    { step: "<img src=x onerror=alert(1)>", status: "running", elapsed_ms: 4000 }
  ] };
  await ready(h, value);
  const table = h.get("ut-d-progress-steps").innerHTML;
  // Each record sits under its pipeline stage; only state intervals add up.
  assert.match(table, /排队.*ut-stage-time">用时 0 毫秒<.*步骤用时 · 1 项.*排队调度等待（等待）.*0 毫秒/);
  assert.match(table, /data-keep="stage:2"><summary>步骤用时 · 3 项/);
  assert.match(table, /LLVM 编译、执行与校验.*第 1 版.*1.25 秒/);
  assert.match(table, /补用例模型请求.*第 2 版 · 第 1 轮.*调用失败.*2.50 秒/);
  assert.match(table, /已用 4.00 秒/);
  assert.doesNotMatch(table, /<img/);
  assert.match(table, /&lt;img/);
  h.click("ut-d-refresh"); await settle();
  h.details.at(-1).reject(new Error("offline")); await settle();
  assert.equal(h.get("ut-d-progress-steps").innerHTML, table);
  assert.match(h.get("ut-d-sync").textContent, /刷新失败/);
});

test("shared and function timing use independent timestamps and stop extrapolating stale observations", async () => {
  const h = harness(), value = job(null, "generating");
  value.timing = { history_complete: false, generation: {
    batch_id: 8, observed_at: "2026-10-26T00:00:10Z",
    project_updated_at: "2026-10-26T00:00:05Z", function_updated_at: "2026-10-25T23:59:00Z",
    shared: [{ phase: "extracting", status: "running", elapsed_ms: 1000 }],
    function: [{ phase: "solving", status: "running", elapsed_ms: 2000 },
      { phase: "validating", status: "interrupted", duration_ms: 999999 }]
  } };
  await ready(h, value);
  const table = h.get("ut-d-progress-steps").innerHTML;
  assert.match(table, /data-keep="stage:1"><summary>步骤用时 · 3 项/);
  assert.match(table, /Clang 抽取（整批共享）.*整批 #8.*已用 6.00 秒/);
  assert.match(table, /求解测试义务.*记录未更新.*已用 2.00 秒/);
  assert.match(table, /校验用例集.*中断，结束时间未知.*未记录/);
  assert.match(h.get("ut-d-timing-note").textContent, /历史耗时不完整.*不能相加/);
});

test("legacy timing is unknown instead of a fabricated zero", async () => {
  const h = harness();
  await ready(h);
  assert.doesNotMatch(h.get("ut-d-progress-steps").innerHTML, /步骤用时|ut-stage-time/);
  assert.match(h.get("ut-d-timing-note").textContent, /旧记录不补算为 0/);
});

test("typed Host outputs have a distinct origin and remain pending before reexecution", async () => {
  const h = harness(), value = job(20, "executing");
  value.last_verdict = null;
  Object.assign(value.attempts[1], { origin: "HOST_DERIVED", verdict: null, check: null });
  await ready(h, value);
  assert.match(h.get("ut-d-attempts").innerHTML, /类型推导/);
  assert.equal(h.get("ut-d-review-section").hidden, true);
});

test("running progress shows batch generation honestly and hands off to review", async () => {
  const h = harness();
  await ready(h, { ...job(null, "generating"), attempts: [], last_verdict: null });
  assert.equal(h.get("ut-d-progress").hidden, false);
  assert.equal(h.get("ut-d-review-section").hidden, true);
  assert.match(h.get("ut-d-progress-summary").textContent, /本批次.*尚未返回.*资源等待/);
  assert.match(h.get("ut-d-progress-steps").innerHTML, /aria-current="step"[^]*用例生成/);
  assert.match(h.get("ut-d-sync").textContent, /每 3 秒自动刷新.*最近成功同步/);
  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve(job());
  await settle();
  assert.equal(h.get("ut-d-review-section").hidden, false);
  assert.equal(h.get("ut-d-gate-forms").hidden, false);
  assert.doesNotMatch(h.get("ut-d-progress-steps").innerHTML, /aria-current/);
  assert.match(h.get("ut-d-progress-steps").innerHTML, /未触发模型修正/);
});

test("generation shows the live ut-agent snapshot and calls the step ratio a budget", async () => {
  const h = harness();
  const generating = { ...job(null, "generating"), attempts: [], last_verdict: null };
  await ready(h, { ...generating, generation_progress: {
    observed_at: "2026-10-23T04:00:30Z",
    project: { phase: "extracting", target_count: 3 }, function: null } });
  assert.match(h.get("ut-d-progress-steps").innerHTML, /正在抽取源码结构（整批 3 个函数/);
  assert.match(h.get("ut-d-progress-summary").textContent, /正在抽取源码结构/);

  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve({ ...generating, generation_progress: {
    observed_at: "2026-10-23T04:00:30Z", project: { phase: "generating" },
    function: { phase: "solving", obligations_done: 3, obligations_total: 8,
      steps_used: 62500000, step_limit: 250000000, updated_at: "2026-10-23T04:00:29Z" } } });
  await settle();
  const steps = h.get("ut-d-progress-steps").innerHTML;
  assert.match(h.get("ut-d-progress-summary").textContent, /正在生成：求解测试义务 · 测试义务 3 \/ 8/);
  assert.match(steps, /<progress class="ut-gen-meter" max="250000000" value="62500000">/);
  assert.match(steps, /语义步数 63M \/ 250M（预算占比 25%）/);
  assert.match(steps, /不代表剩余时间/);
  assert.doesNotMatch(steps, /未更新/);

  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve({ ...generating, generation_progress: {
    observed_at: "2026-10-23T04:05:00Z", project: { phase: "generating" },
    function: { phase: "solving", obligations_done: 3, obligations_total: 8,
      steps_used: 62500000, step_limit: 250000000, updated_at: "2026-10-23T04:00:29Z" } } });
  await settle();
  assert.match(h.get("ut-d-progress-steps").innerHTML, /进度已 271 秒未更新/);
});

test("finished generation keeps its step count and names budget exhaustion", async () => {
  const h = harness();
  await ready(h, { ...job(null, "needs_human"), attempts: [], last_verdict: null,
    gate_code: "NO_TESTCSV", reason: "生成器未产出 TestCsv（FAILED）",
    generation: { semantic_steps: 250000001, step_limit: 250000000,
      error: "GenerationBudgetExceeded: GENERATION_BUDGET_EXHAUSTED: f" } });
  const steps = h.get("ut-d-progress-steps").innerHTML;
  assert.match(steps, /语义步数 250M \/ 250M（预算占比 100%）/);
  assert.match(steps, /超出语义步数预算，生成已安全停止/);
});

test("repair rounds remain a loop and return to execution without claiming a pass", async () => {
  const h = harness();
  await ready(h, { ...job(10, "repairing"), repair_rounds: 2, last_verdict: "FAIL" });
  assert.match(h.get("ut-d-progress-steps").innerHTML, /已完成修正 2 \/ 5 轮.*正在修正/);
  assert.match(h.get("ut-d-progress-summary").textContent, /返回执行与校验/);
  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve({ ...job(20, "executing"), repair_rounds: 3, last_verdict: "FAIL" });
  await settle();
  assert.match(h.get("ut-d-progress-steps").innerHTML, /已完成修正 3 \/ 5 轮/);
  assert.match(h.get("ut-d-progress-summary").textContent, /资源等待/);
  assert.equal(h.get("ut-d-review-section").hidden, true);
});

test("failure before generation does not fabricate completed generation or execution", async () => {
  const h = harness();
  await ready(h, { ...job(null, "error"), attempts: [], last_verdict: null,
    gate_code: "GENERATION_FAILED", reason: "生成失败：工具异常 <script>" });
  const steps = h.get("ut-d-progress-steps").innerHTML;
  assert.match(steps, /生成失败/);
  assert.match(steps, /无校验记录/);
  assert.doesNotMatch(steps, /is-done|aria-current/);
  assert.equal(h.get("ut-d-progress-summary").textContent, "生成失败：工具异常 <script>");
  assert.equal(h.get("ut-d-review-section").hidden, false);
  assert.equal(h.get("ut-d-approve").disabled, true);
});

test("failed detail sync preserves the snapshot and recovers on the next poll", async () => {
  const h = harness();
  await ready(h, job(10, "executing"));
  const snapshot = h.get("ut-d-progress-steps").innerHTML;
  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).reject(new Error("detail offline"));
  await settle();
  assert.equal(h.get("ut-d-progress-steps").innerHTML, snapshot);
  assert.match(h.get("ut-d-sync").textContent, /刷新失败，当前为上次状态.*最近成功同步/);
  assert.equal(h.get("ut-d-approve").disabled, true);
  h.timers.at(-1)();
  await settle();
  h.details.at(-1).resolve(job(20));
  await settle();
  assert.doesNotMatch(h.get("ut-d-sync").textContent, /刷新失败/);
  assert.equal(h.get("ut-detail-status").textContent, "");
  assert.equal(h.get("ut-d-approve").disabled, false);
});

test("a list refresh failure marks displayed detail stale without losing its evidence", async () => {
  const h = harness();
  await ready(h);
  h.failList();
  await h.click("ut-d-refresh");
  assert.match(h.get("ut-d-sync").textContent, /刷新失败，当前为上次状态/);
  assert.equal(h.get("ut-d-download").href, "/csv/1/10");
  assert.equal(h.get("ut-d-approve").disabled, true);
});

test("old detail errors cannot mark another job's progress stale", async () => {
  const h = harness();
  await ready(h, job(10, "executing"));
  h.click("ut-d-refresh");
  await settle();
  const old = h.details.at(-1);
  h.open(2);
  h.details.at(-1).resolve({ ...job(20, "executing"), id: 2 });
  await settle();
  old.reject(new Error("old job offline"));
  await settle();
  assert.doesNotMatch(h.get("ut-d-sync").textContent, /刷新失败/);
  assert.equal(h.get("ut-d-download").href, "/csv/2/20");
});

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

test("the check panel follows the latest candidate without fetching CSV text", async () => {
  const h = harness();
  await ready(h);
  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve(job(20));
  await settle();
  assert.match(h.get("ut-d-issues-title").textContent, /第 2 版.*当前版本/);
  assert.equal(h.previews.length, 0);
});

test("historical preview is explicit and cannot approve the unseen current candidate", async () => {
  const h = harness();
  await ready(h, job(20));
  h.pin(10);
  assert.match(h.get("ut-d-issues-title").textContent, /历史版本/);
  assert.equal(h.get("ut-d-approve").disabled, true);
  h.pin(20);
  assert.equal(h.get("ut-d-approve").disabled, false);
});

const CSV = 'mod,"a.c/f","f test",1,1,,,,CPP,,,"",0\r\n#COMMENT,"a.c/x","a.c/f@@"\r\n' +
  ";$L$,if (x)\r\n;$L$,TRUE\r\n,1,1\r\n;$L$,FALSE\r\n,0,0\r\n";

test("viewing a candidate opens the drawer read-only with only comments and values", async () => {
  const h = harness();
  await ready(h);
  h.pin(10, "view");
  await settle();
  h.previews.at(-1).resolve({ text: CSV });
  await settle();
  const { item, opts } = h.sheets.at(-1);
  assert.equal(opts.readOnly, true);
  assert.equal(opts.onSave, null);
  assert.match(opts.heading, /CSV · 第 1 版（生成器）/);
  assert.deepEqual(JSON.parse(JSON.stringify(opts.layout.left)), []);
  assert.equal(opts.layout.commentField, "purpose");
  assert.equal(opts.layout.autoRowHeight, false);
  assert.equal(item.steps.steps[0].purpose, "if (x)\nTRUE");
});

test("successful revision followed by a failed refresh keeps review disabled", async () => {
  const h = harness();
  await ready(h);
  h.click("ut-d-refresh");
  await settle();
  const stale = h.details.at(-1);
  h.click("ut-d-edit-sheet");
  await settle();
  h.previews.at(-1).resolve({ text: CSV });
  await settle();
  const { item, opts } = h.sheets.at(-1);
  assert.equal(opts.readOnly, false);
  const doc = JSON.parse(JSON.stringify(item.steps));
  doc.steps[1].expecteds = ["1"];
  const saved = opts.onSave("", doc);
  h.failList();
  h.revisions[0].resolve({});
  await saved;
  await settle();
  assert.match(h.revisions[0].args[2], /\r\n,0,1\r\n$/);
  stale.resolve(job());
  await settle();
  assert.equal(h.get("ut-d-gate-forms").hidden, true);
  assert.equal(h.get("ut-d-approve").disabled, true);
  assert.match(h.get("ut-detail-status").textContent, /offline/);
});

test("an unchanged table is not submitted as a revision", async () => {
  const h = harness();
  await ready(h);
  h.click("ut-d-edit-sheet");
  await settle();
  h.previews.at(-1).resolve({ text: CSV });
  await settle();
  const { item, opts } = h.sheets.at(-1);
  await assert.rejects(opts.onSave("", item.steps), /没有改动/);
  assert.equal(h.revisions.length, 0);
});

test("check issues render as structured evidence instead of raw JSON", async () => {
  const h = harness();
  const value = job();
  value.attempts[0].verdict = "FAIL";
  value.attempts[0].check = { verdict: "FAIL", issues: [
    { code: "ORACLE_MISMATCH", severity: "error", message: "1 个期望值与 WinAMS 实际值不一致",
      detail: [{ row: 0, column: "a.c/f@@", expected: "1", actual: 0 }] },
    { code: "COVERAGE_C0_INCOMPLETE", severity: "error", message: "C0 50%",
      detail: { c0: 50, c0_ratio: [1, 2], c1: null, c1_ratio: [0, -1], mcdc: null } },
  ] };
  await ready(h, value);
  const first = h.get("ut-d-issues").innerHTML;
  assert.match(first, /data-issue-tab="ORACLE_MISMATCH"[^>]*>.*1 个期望值.*<span class="ut-tab-n">1</);
  assert.doesNotMatch(first, /C0<\/b> 50%/);
  const html = allIssues(h);
  assert.match(html, /<table class="ut-issue-table">[\s\S]*#1[\s\S]*f@@/);
  assert.match(html, /C0<\/b> 50%（1 \/ 2）/);
  assert.match(html, /C1<\/b> 未测量/);
  assert.doesNotMatch(html, /\{&quot;|\{"/);
  assert.equal(h.get("ut-d-issues-sum").textContent, "· 2 个错误");
});

test("host evidence keeps line/branch metrics, unknown MC/DC and stub artifacts explicit", async () => {
  const h = harness();
  const value = job();
  value.attempts[0].execution = { evidence_source: "HOST_EXECUTION", status: "HOST_PASS",
    counts_as_target_evidence: false,
    coverage: { lines: { percent: 75, covered: 3, total: 4 },
      branches: { percent: 50, covered: 1, total: 2 }, mcdc: { status: "UNSUPPORTED" } },
    stub_generation: { mode: "llm", model: "<stub-model>",
      rounds: [{ round: 1, status: "HOST_PASS", usage: { input_tokens: 10, output_tokens: 5 } }],
      artifacts: [{ id: "abc", name: "round-1/proposal.json", sha256: "abcdef" }] } };
  await ready(h, value);
  assert.equal(h.get("ut-d-host").hidden, false);
  assert.match(h.get("ut-d-host-summary").innerHTML, /行<\/b> 75%（3 \/ 4）/);
  assert.match(h.get("ut-d-host-summary").innerHTML, /分支<\/b> 50%（1 \/ 2）/);
  assert.match(h.get("ut-d-host-summary").innerHTML, /MC\/DC<\/b> UNSUPPORTED/);
  assert.doesNotMatch(h.get("ut-d-host-summary").innerHTML, /C0|C1|100%/);
  assert.match(h.get("ut-d-stub-rounds").innerHTML, /&lt;stub-model&gt;[\s\S]*10 \/ 5/);
  assert.match(h.get("ut-d-host-artifacts").innerHTML, /href="\/artifact\/1\/10\/abc"/);
});

function coverageReport() {
  return { attempt: { id: 10, seq: 1, verdict: "PASS" }, report: {
    contract: "ut-agent.coverage-lines/1", evidence_source: "HOST_EXECUTION", cases: 3,
    summary: { c0: { covered: 8, total: 9, percent: 88.9 }, c1: { covered: 3, total: 4, percent: 75 },
      mcdc: { covered: 1, total: 2, percent: 50 } },
    c1_basis: "DECISION", c1_reason: "", mcdc_reported: true, source_sha256: "0123456789abcdef",
    lines: [
      { line: 2, text: "int classify(int a, int b)", count: null, c0: null, c1: [], mcdc: [] },
      { line: 4, text: "\tif (a > 0 && b > 0) {", count: 2, c0: "COVERED",
        c1: [{ column: 9, kind: "DECISION", true: 1, false: 1, state: "COMPLETE" }],
        mcdc: [{ column: 9, conditions: 2, shown: 1, uncovered: [2], state: "PARTIAL" }] },
      { line: 7, text: "x < <y>", count: 1, c0: "COVERED",
        c1: [{ column: 9, kind: "DECISION", true: 1, false: 0, state: "PARTIAL" }], mcdc: [] },
      { line: 10, text: "return -1;", count: 0, c0: "NOT_COVERED", c1: [], mcdc: [] },
    ], unplaced: [
      { line: 30, origin: "OUTSIDE_SHOWN_LINES", c1: [], mcdc: [{ column: 3, state: "EXCLUDED", reason: "macro" }] },
      { line: 3, origin: "MACRO_EXPANSION", c1: [{ column: 20, kind: "DECISION", true: 1, false: 0, state: "PARTIAL" }],
        mcdc: [] },
    ] } };
}

function coverageJob(constraints = { coverage_policy: { c0: true, c1: true, mcdc: true } }) {
  const value = job();
  value.batch = { constraints };
  value.attempts[0].execution = { evidence_source: "HOST_EXECUTION", status: "HOST_PASS",
    has_coverage_report: true, coverage: { lines: { percent: 88.9, covered: 8, total: 9 } } };
  return value;
}

test("the selected version's host coverage report shows each line's C0, C1 and MC/DC", async () => {
  const h = harness();
  const value = coverageJob();
  await ready(h, value);
  assert.equal(h.get("ut-d-coverage").hidden, false);
  assert.equal(h.coverages.length, 1);
  assert.deepEqual([h.coverages[0].job, h.coverages[0].attempt], [1, 10]);
  h.coverages[0].resolve(coverageReport());
  await settle();
  const html = h.get("ut-d-coverage-body").innerHTML;
  assert.match(html, /Clang\/LLVM 实测覆盖率（HOST_EXECUTION），按项目测试要求展示/);
  assert.doesNotMatch(html, /仅开发期证据|不作为目标覆盖率|实验/);
  assert.match(html, /C0（行）<\/b> 88.9%（8 \/ 9）/);
  assert.match(html, /C1（判定）<\/b> 75%（3 \/ 4）/);
  assert.match(html, /MC\/DC<\/b> 50%（1 \/ 2）/);
  assert.match(html, /未执行的行：10 · 未满足覆盖要求的判定所在行：4, 7/);
  assert.match(html, /仅未满足/);
  assert.match(html, /红色：未执行/);
  assert.match(html, /橙色：C1 \/ MC\/DC 未满足/);
  assert.match(html, /False 分支未执行/);
  // Columns stay on the source row. The MC/DC tip lists every llvm-cov condition.
  assert.match(html, /<tr class="is-miss"><td class="ut-num">10<\/td><td><span class="is-miss">\u00d7/);
  assert.match(html, /1 \/ 2<span class="ut-mcdc-tip"/);
  assert.match(html, /条件 1：已证明独立影响/);
  assert.match(html, /class="is-miss">条件 2：未证明独立影响/);
  assert.match(html, /T\u2713<\/span><span class="is-miss">F\u2717/);
  assert.match(html, /x &lt; &lt;y&gt;/);
  assert.match(html, /函数范围外的记录：第 30 行/);
  assert.match(html, /宏展开的判定（行号属于宏定义文件）：第 3 行/);

  // Filters re-render from the cached report; a poll does not refetch it.
  h.get("ut-d-coverage").listeners.click({ target: { closest: () => ({ dataset: { covView: "gaps" } }) } });
  const gaps = h.get("ut-d-coverage-body").innerHTML;
  assert.doesNotMatch(gaps, /classify/);
  assert.match(gaps, /<td class="ut-num">7<\/td>/);
  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve(value);
  await settle();
  assert.equal(h.coverages.length, 1);
  assert.equal(h.get("ut-d-coverage-body").innerHTML, gaps);
});

test("row highlights and the unmet filter follow the frozen batch requirements", async () => {
  for (const [constraints, expected] of [
    [{ coverage_policy: { c0: true, c1: true, mcdc: true } }, [4, 7, 10]],
    [{ coverage_policy: { c0: true, c1: false, mcdc: false } }, [10]],
    [{ coverage_policy: { c0: false, c1: true, mcdc: false } }, [7]],
    [{ coverage_policy: { c0: false, c1: false, mcdc: true } }, [4]],
    [{ coverage_policy: { c0: false, c1: false, mcdc: false } }, []],
    [{ coverage_policy: { c0: true, c1: true, mcdc: true },
      test_policy: { c1: true, mcdc: true }, effective_test_policy: { c1: false, mcdc: false } }, [10]],
    [{ coverage_policy: { c0: true }, test_policy: { c1: false, mcdc: true } }, [4, 10]],
    [{}, []],
  ]) {
    const h = harness();
    await ready(h, coverageJob(constraints));
    h.coverages[0].resolve(coverageReport());
    await settle();
    const html = h.get("ut-d-coverage-body").innerHTML;
    const highlighted = [...html.matchAll(/<tr class="is-(?:miss|gap)"><td class="ut-num">(\d+)/g)]
      .map((m) => Number(m[1]));
    assert.deepEqual(highlighted, expected, JSON.stringify(constraints));
    h.get("ut-d-coverage").listeners.click({ target: { closest: () => ({ dataset: { covView: "gaps" } }) } });
    const filtered = h.get("ut-d-coverage-body").innerHTML;
    const rows = [...filtered.matchAll(/<tr class="[^"]*"><td class="ut-num">(\d+)/g)].map((m) => Number(m[1]));
    assert.deepEqual(rows, expected, "the filter and the row colors must identify the same gaps");
    if (!expected.length) assert.match(filtered, /没有已测得的未满足行/);
    if (!Object.keys(constraints).length) assert.match(html, /要求未绑定/);
    if (constraints.effective_test_policy) {
      assert.match(html, /MC\/DC<\/b> 50%（1 \/ 2） · 未要求/);
      assert.match(html, /未满足覆盖要求的判定所在行：无/);
    }
  }
});

test("unexecuted rows take precedence while unknown, excluded and non-code rows stay neutral", async () => {
  const h = harness();
  await ready(h, coverageJob());
  const data = coverageReport();
  data.report.lines[3].c1 = [{ column: 2, kind: "DECISION", true: 0, false: 0, state: "NONE" }];
  data.report.lines.push(
    { line: 11, text: "return 1;", count: 2, c0: "COVERED", c1: [], mcdc: [] },
    { line: 12, text: "macro();", count: 2, c0: "COVERED", c1: [],
      mcdc: [{ column: 2, state: "EXCLUDED", reason: "unsupported <macro>" }] },
    { line: 13, text: "decision();", count: null, c0: null,
      c1: [{ column: 2, state: "UNKNOWN", reason: "missing <evidence>" }],
      mcdc: [{ column: 2, state: "UNKNOWN", reason: "not instrumented" }] },
    { line: 14, text: "// comment", count: null, c0: null, c1: [], mcdc: [] });
  h.coverages[0].resolve(data);
  await settle();
  const html = h.get("ut-d-coverage-body").innerHTML;
  assert.match(html, /<tr class="is-miss"><td class="ut-num">10/);
  assert.match(html, /<tr class="is-gap"><td class="ut-num">4/);
  assert.match(html, /<tr class="is-gap"><td class="ut-num">7/);
  for (const line of [2, 11, 12, 13, 14]) {
    assert.ok(html.includes('<tr class=""><td class="ut-num">' + line + '</td>'));
  }
  assert.match(html, /class="ut-cov-mark is-missing" title="unsupported &lt;macro&gt;">—/);
  assert.match(html, /class="ut-cov-mark is-missing" title="missing &lt;evidence&gt;">—/);
  assert.match(html, /class="ut-cov-mark is-missing" title="not instrumented">—/);
  assert.doesNotMatch(html, /undefined/);
});

test("the MC/DC tip gives one line per condition: its true vector in the decision's structure and the pair", async () => {
  const h = harness();
  const value = coverageJob();
  await ready(h, value);
  const data = coverageReport();
  data.report.lines[1].mcdc = [{ column: 7, conditions: 3, shown: 2, uncovered: [3], state: "PARTIAL",
    end: [4, 22], text: "BOTH(a, d) || b<x>", formula: ["(", 1, "&&", 3, ")", "||", 2], formula_reason: "",
    condition_details: [
      { number: 1, independent: true, pair: [1, 3], line: 1, column: 21, text: "(x)", macro: true },
      { number: 2, independent: true, pair: [1, 2], line: 4, column: 21, text: "b", macro: false },
      { number: 3, independent: false, pair: null, line: null, column: null, text: null, macro: false }],
    test_vectors: [{ number: 1, conditions: [false, false, null], result: false },
      { number: 2, conditions: [false, true, null], result: true },
      { number: 3, conditions: [true, null, true], result: true }],
    detail_reason: "condition C3 matches 0 branch regions" }];
  data.report.unplaced[1].mcdc = [{ column: 19, conditions: 2, shown: 0, uncovered: [1, 2], state: "NONE",
    text: "(x) > 0 || (y) > 0", formula: null, formula_reason: "no IR decision", condition_details: [
      { number: 1, independent: true, pair: [1, 2], line: 2, column: 19, text: "(x) > 0", macro: true },
      { number: 2, independent: false, pair: null, line: 2, column: 30, text: "(y) > 0", macro: true }],
    test_vectors: [{ number: 1, conditions: [false, false], result: false },
      { number: 2, conditions: [true, false], result: true }], detail_reason: "" }];
  h.coverages[0].resolve(data);
  await settle();
  const html = h.get("ut-d-coverage-body").innerHTML;
  assert.match(html, /<span class="ut-mcdc-where">第 7 列<\/span>/);
  assert.doesNotMatch(html, /BOTH\(a, d\)/);
  // Condition 1 is true in vector 3 of its pair; condition 2 in vector 2.
  assert.match(html, /class="ut-mcdc-tv is-full" title="\(x\)（宏定义内第 1 行）">条件1：\(T&amp;&amp;T\)\|\|- =&gt; T  ✓  向量 1↔3</);
  assert.match(html, /class="ut-mcdc-tv is-full" title="b">条件2：\(F&amp;&amp;-\)\|\|T =&gt; T  ✓  向量 1↔2</);
  assert.match(html, /class="ut-mcdc-tv is-miss" title="位置未知">条件3：✗ 未证明独立影响</);
  assert.match(html, /condition C3 matches 0 branch regions/);
  assert.doesNotMatch(html, /已执行的测试向量|条件 1：/);
  // Without a formula the vector is listed in condition order; the reason is on the header.
  assert.match(html, /宏展开的判定（行号属于宏定义文件）：第 3 行 · 第 3 行 MC\/DC <span class="ut-cov-mark ut-mcdc is-miss"/);
  assert.match(html, /<span class="ut-mcdc-where" title="no IR decision">第 19 列<\/span>/);
  assert.match(html, /条件1：\{T, F\} =&gt; T  ✓  向量 1↔2/);
});

test("a version without a host report hides the coverage section and ignores late replies", async () => {
  const h = harness();
  const value = job(20);
  value.attempts[0].execution = { evidence_source: "HOST_EXECUTION", has_coverage_report: true };
  value.attempts[1].execution = { evidence_source: "HOST_EXECUTION", has_coverage_report: false };
  await ready(h, value);
  assert.equal(h.get("ut-d-coverage").hidden, true);
  h.pin(10);
  assert.equal(h.get("ut-d-coverage").hidden, false);
  h.pin(20);
  h.coverages[0].resolve(coverageReport());
  await settle();
  assert.equal(h.get("ut-d-coverage").hidden, true);
  assert.equal(h.get("ut-d-coverage-body").innerHTML, "");
});

test("input-completeness gaps expose readable goals and full original evidence", async () => {
  const h = harness();
  const value = job();
  value.attempts[0].verdict = "PASS";
  value.attempts[0].check = { verdict: "PASS", issues: [
    { code: "INPUT_OBLIGATION_UNRESOLVED", severity: "warning", message: "1 个 Baseline 义务没有可执行的输入行",
      detail: [{ obligation_id: "b6:boundary:1:above:2", kind: "boundary", branch_id: "b6",
                 decision: "( A ==\n  b )", description: "atom 1", outcome: null, condition_index: 1,
                 boundary_class: "above", boundary_value: 2, boundary_right_value: null,
                 case_label: null, rule_id: "psd.4.compare", solve_status: "UNKNOWN",
                 reason: "volatile read", intent_status: "NEEDS_REVIEW" }] },
  ] };
  await ready(h, value);
  const html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /ut-gap-card[\s\S]*b6:boundary:1:above:2/);
  assert.match(html, /边界值 · 条件 1 · 边界上方 2/);
  assert.match(html, /判定／要求<\/dt><dd>\( A == b \)/);
  assert.match(html, /UNKNOWN[\s\S]*volatile read/);
  assert.match(html, /本页原始诊断[\s\S]*psd\.4\.compare[\s\S]*intent_status/);
  assert.match(html, /本报告未提供处理指引/);
  assert.equal(h.get("ut-d-issues-sum").textContent, "· 1 个警告");
});

test("missing runtime asks for an environment retry instead of source location evidence", async () => {
  const h = harness();
  const value = job();
  value.attempts[0].check = { verdict: "BLOCKED", issues: [
    { code: "HOST_COVERAGE_REQUIRED_UNKNOWN", severity: "warning",
      detail: { metric: "c1", reason: "runtime missing <unsafe>", locations: [],
        reason_code: "HOST_PROFILE_RUNTIME_UNAVAILABLE" } },
  ] };
  await ready(h, value);
  const html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /修复运行库后重试执行/);
  assert.match(html, /runtime missing &lt;unsafe&gt;/);
  assert.doesNotMatch(html, /请先补测量或位置证据/);
});

test("diagnostic guidance keeps actionable steps uncut, escapes evidence and downloads the selected version", async () => {
  const h = harness();
  const value = job(20);
  const longReason = "读取证据".repeat(70) + "结尾证据";
  const guidance = { title: "缺少输入证明", action_scope: "SEMANTIC_EVIDENCE",
    steps: ["补齐类型化读取契约", "<script>unsafe</script>"], verification: "重新提取与校验" };
  const oldCheck = { verdict: "PASS", delivery_validated: false, diagnostic_contract: "ut-agent.check-guidance/1", issues: [
    { code: "INPUT_OBLIGATION_UNRESOLVED", severity: "warning", message: "75 个测试要求未解决", guidance,
      detail: [{ obligation_id: "B1", kind: "boundary", solve_status: "UNKNOWN", reason: longReason,
        gap_class: "SEMANTIC_EVIDENCE", constraints: [{ variable: "x", operator: "<", value: 3 }], guidance }] },
    { code: "HOST_COVERAGE_REQUIRED_UNKNOWN", severity: "warning", guidance,
      detail: { metric: "c1", locations: [{ branch_id: "B2", line: 42, outcome: false, state: "UNKNOWN", reason: "宏映射未知" }] } },
    { code: "HOST_STUB_OBSERVATION_MISMATCH", severity: "error", guidance,
      detail: { build: null, cases: [], observation_differences: [{ case_id: "A", column: "extra",
        reference: { kind: "i", value: 1 }, candidate: { kind: "i", value: 2 } }] } },
  ] };
  oldCheck.completeness = { unresolved_count: 75, unresolved: oldCheck.issues[0].detail };
  value.attempts[0].check = oldCheck;
  value.attempts[0].csv_sha256 = "old-hash";
  value.attempts[0].execution = { evidence_source: "HOST_EXECUTION", counts_as_target_evidence: false };
  await ready(h, value);
  h.pin(10);
  const html = allIssues(h);
  assert.match(html, /处理入口[\s\S]*语义／环境证据[\s\S]*补齐类型化读取契约/);
  assert.match(html, /如何确认完成[\s\S]*重新提取与校验/);
  assert.ok(html.includes(longReason));
  assert.match(html, /x &lt; 3/);
  assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /源码行[\s\S]*42[\s\S]*假[\s\S]*无法判定/);
  assert.match(html, /75 个测试要求未解决/);
  assert.match(html, /共 75 项未解决测试要求，本报告只提供前 1 项明细；另有 74 项/);
  assert.match(html, /参考桩观测[\s\S]*候选桩观测/);
  h.get("ut-d-issues").listeners.click({ target: { closest(selector) {
    return selector === "[data-diagnostic]" ? { dataset: { diagnostic: "10" } } : null;
  } } });
  assert.equal(h.diagnostics[0].attempt, 10);
  h.diagnostics[0].resolve({ contract: "ut-platform.diagnostic-packet/2", job_id: 1, attempt_id: 10,
    version: { seq: 1 }, candidate_sha256: "old-hash", check: oldCheck,
    execution: value.attempts[0].execution, instruction: "证据不足" });
  await settle();
  const packet = JSON.parse(await h.downloads[0].text());
  assert.equal(packet.attempt_id, 10);
  assert.equal(packet.candidate_sha256, "old-hash");
  assert.deepEqual(packet.check, oldCheck);
  assert.equal(packet.execution.counts_as_target_evidence, false);
  assert.match(packet.instruction, /证据不足/);
  assert.match(h.downloads[1].download, /1-10\.json$/);
  assert.equal(h.revisions.length, 0);
  assert.equal(h.reviews.length, 0);
});

test("check requirements show enabled, disabled and presentation selections even without errors", async () => {
  const h = harness();
  const value = job();
  value.attempts[0].check.test_requirements = { status: "BOUND", rules_ref: "universal-test-rules@1", requirements: [
    { key: "fixed_boundary", label: "最大值、最小值与境界值本身", required: true, role: "TEST_CASE" },
    { key: "c1", label: "C1 分支结果覆盖", required: true, role: "TEST_CASE" },
    { key: "mcdc", label: "MC/DC 独立影响用例", required: false, role: "TEST_CASE" },
    { key: "combination_group", label: "MC/DC 組合せ注释呈现", required: false, role: "PRESENTATION" },
  ] };
  await ready(h, value);
  const html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /本次生成绑定的测试要求[\s\S]*universal-test-rules@1/);
  assert.match(html, /C1 分支结果覆盖：<b>要求/);
  assert.match(html, /MC\/DC 独立影响用例：<b>未要求/);
  assert.match(html, /MC\/DC 組合せ注释呈现：<b>关闭，不要求此类注释/);
  assert.match(html, /未报告校验问题/);
});

function issueAction(h, selector, dataset) {
  h.get("ut-d-issues").listeners.click({ target: { closest(s) { return s === selector ? { dataset } : null; } } });
}
function typedCheck(count = 65) {
  const gaps = Array.from({ length: count }, (_, i) => ({ obligation_id: `B${i}`, gap_class: "MISSING_CASE", kind: "branch" }));
  return { candidate_sha256: "sha10", completeness: { unresolved: gaps, unresolved_count: count, details_complete: true,
      output_pending: Array.from({ length: 61 }, (_, i) => ({ obligation_id: `O${i}`, pending_outputs: ["f@@"] })), output_pending_count: 61 },
    issues: [{ code: "INPUT_OBLIGATION_UNRESOLVED", severity: "warning", detail: gaps.slice(0, 50) },
      { code: "OUTPUT_ORACLE_PENDING", severity: "warning", detail: [] }],
    input_ranges: { contract: { contract: "ut-agent.input-domains/1", columns: [
      { index: 1, column: "a.c/x", status: "PROVEN", min: 0, max: 255 }, { index: 2, column: "a.c/y", status: "UNKNOWN" }] } },
    candidate_witnesses: { contract: "ut-agent.candidate-witness/1", evidence: { identified: true }, input_mapping_identified: true, candidate_sha256: "sha10",
      repair_context: [{ obligation_id: "B0", path: [
        { branch_id: "ancestor", decision: "x < 3", required_outcome: false,
          location: { file: "src/<unsafe>.c", line: 12, column: 4 },
          atoms: [{ variable: "x", operator: "<", boundary: 3, input_tokens: ["I1"], binding: "DIRECT_INPUT" }] },
        { branch_id: "child", decision: "rom > y", required_outcome: true, location: { file: "src/a.c", line: 24 },
          atoms: [{ variable: "rom", right_operand: "y", binding: "NO_DIRECT_INPUT", input_tokens: [], right_input_tokens: ["I2"] }] },
      ] }] } };
}
function packetFor(a, extra = {}) {
  return { contract: "ut-platform.diagnostic-packet/2", job_id: 1, attempt_id: a.id,
    version: { seq: a.seq }, candidate_sha256: a.csv_sha256, check: a.check,
    inputs: { rows_complete: true, rows: [{ base_row: 1, inputs: { I1: "1", I2: "2" } }] }, ...extra };
}

function producerTypedCheck() {
  const check = typedCheck(1);
  check.candidate_witnesses.repair_context[0].path[0].atoms = [
    { variable: "x", operator: ">", boundary: null, right_operand: "y",
      right_binding: "DIRECT_INPUT", right_control_sources: ["param"],
      input_tokens: ["I1"], binding: "DIRECT_INPUT", control_sources: ["param"],
      right_input_tokens: ["I2"], mask_variable: null, mask: null,
      mask_binding: "NOT_APPLICABLE", mask_control_sources: [], mask_input_tokens: [] },
    { variable: "x", operator: "!=", boundary: 0, right_operand: null,
      right_binding: "NOT_APPLICABLE", right_control_sources: [],
      input_tokens: ["I1"], binding: "DIRECT_INPUT", control_sources: ["param"],
      right_input_tokens: [], mask_variable: "y", mask: null,
      mask_binding: "DIRECT_INPUT", mask_control_sources: ["param"], mask_input_tokens: ["I2"] },
  ];
  return check;
}

test("full machine gap and output lists paginate beyond the 50-item issue preview", async () => {
  const h = harness(), value = job();
  value.attempts[0].csv_sha256 = "sha10";
  value.attempts[0].check = typedCheck();
  await ready(h, value);
  let html = issueTab(h, "INPUT_OBLIGATION_UNRESOLVED", 10);
  assert.match(html, /data-issue-tab="INPUT_OBLIGATION_UNRESOLVED".*<span class="ut-tab-n">65</);
  assert.match(html, /data-issue-tab="OUTPUT_ORACLE_PENDING".*<span class="ut-tab-n">61</);
  assert.equal((html.match(/class="ut-gap-card"/g) || []).length, 8);
  assert.match(html, /显示 1–8 \/ 65 项/);
  assert.match(issueTab(h, "OUTPUT_ORACLE_PENDING", 10), /显示 1–8 \/ 61 项/);
  assert.doesNotMatch(allIssues(h), /B64|O60/);
  issueTab(h, "INPUT_OBLIGATION_UNRESOLVED", 10);
  issueAction(h, "[data-gap-page]", { gapVersion: "10", gapKind: "unresolved", gapPage: "8" });
  html = h.get("ut-d-issues").innerHTML;
  assert.equal((html.match(/class="ut-gap-card"/g) || []).length, 1);
  assert.match(html, /显示 65–65 \/ 65 项/);
  assert.match(html, /B64/);
  assert.doesNotMatch(html, /<code>B0<\/code>/);
  issueTab(h, "OUTPUT_ORACLE_PENDING", 10);
  issueAction(h, "[data-gap-page]", { gapVersion: "10", gapKind: "output_pending", gapPage: "7" });
  assert.match(h.get("ut-d-issues").innerHTML, /O60/);
});

test("historical pending-output truncation requests a full recheck", async () => {
  const h = harness(), value = job();
  value.attempts[0].check = typedCheck();
  value.attempts[0].check.completeness.output_pending_count = 71;
  await ready(h, value);
  const html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /共 71 项期望值待回填.*只提供 61 项明细.*另有 10 项未提供/);
  assert.match(html, /重新校验取得完整清单，不能交给模型猜测期望值/);
});

test("typed path shows escaped locations, ancestor decisions, exact operands and explicit unmapped variables", async () => {
  const h = harness(), value = job();
  value.attempts[0].csv_sha256 = "sha10";
  value.attempts[0].check = typedCheck();
  await ready(h, value);
  const html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /src\/&lt;unsafe&gt;\.c:12:4/);
  assert.match(html, /ancestor · 判定：x &lt; 3 · 路径要求：false/);
  assert.match(html, /I1 → a\.c\/x/);
  assert.match(html, /I2 → a\.c\/y/);
  assert.match(html, /NO_DIRECT_INPUT/);
  assert.match(html, /缺用例没有可定位的数据行/);
  assert.match(html, /类型化约束[\s\S]*变量 x · 关系 &lt; · 边界 3 · 绑定 DIRECT_INPUT/);
  assert.match(html, /类型化约束[\s\S]*变量 rom · 关系 UNKNOWN · 边界 UNKNOWN · 右侧操作数 y · 绑定 NO_DIRECT_INPUT/);
  assert.match(html, /<summary>原始类型化原子<\/summary>[\s\S]*&quot;variable&quot;: &quot;x&quot;/);
  assert.doesNotMatch(html, /<b>类型化约束：<\/b>[\s\S]*\{[\s\S]*"variable"/);
  assert.doesNotMatch(html, /<unsafe>/);
});

test("producer RHS and dynamic mask tokens render as readable typed constraints", async () => {
  const h = harness(), value = job();
  value.attempts[0].csv_sha256 = "sha10";
  value.attempts[0].check = producerTypedCheck();
  await ready(h, value);
  const html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /右侧操作数 y（DIRECT_INPUT）/);
  assert.match(html, /掩码变量 y/);
  assert.match(html, /掩码绑定 DIRECT_INPUT/);
  assert.match(html, /I2 → a\.c\/y/);
  assert.doesNotMatch(html, /右侧操作数 UNKNOWN（NOT_APPLICABLE）/);
  assert.match(html, /原始类型化原子[\s\S]*right_input_tokens[\s\S]*mask_input_tokens/);
});

for (const missing of ["evidence", "mapping", "column", "hash", "witness_hash", "witness_contract", "location", "token"]) {
  test(`typed mapping remains explicit when ${missing} evidence is unavailable`, async () => {
    const h = harness(), value = job(), check = typedCheck();
    value.attempts[0].csv_sha256 = "sha10";
    value.attempts[0].check = check;
    if (missing === "evidence") check.candidate_witnesses.evidence.identified = false;
    if (missing === "mapping") check.candidate_witnesses.input_mapping_identified = false;
    if (missing === "column") check.input_ranges.contract.columns[0].index = 9;
    if (missing === "hash") check.candidate_sha256 = "other";
    if (missing === "witness_hash") delete check.candidate_witnesses.candidate_sha256;
    if (missing === "witness_contract") check.candidate_witnesses.contract = "unsupported/2";
    if (missing === "location") delete check.candidate_witnesses.repair_context[0].path[0].location;
    if (missing === "token") check.candidate_witnesses.repair_context[0].path[0].atoms[0].input_tokens = ["I99"];
    await ready(h, value);
    const html = h.get("ut-d-issues").innerHTML;
    assert.match(html, /UNKNOWN/);
    if (!["location", "token"].includes(missing)) {
      assert.doesNotMatch(html, /src\/&lt;unsafe&gt;\.c:12|I1 → a\.c\/x|data-gap-inputs/);
    }
    if (missing === "token") assert.match(html, /I99 → UNKNOWN（无精确列映射）/);
  });
}

test("gap input action marks all existing rows by exact column without fabricated missing-case row", async () => {
  const h = harness(), value = job();
  value.attempts[0].csv_sha256 = "sha10";
  value.attempts[0].check = typedCheck();
  await ready(h, value);
  issueAction(h, "[data-gap-inputs]", { gapAttempt: "10", gapInputs: "0" });
  h.previews[0].resolve({ text: "mod,a.c/f,f,2,1\r\n#COMMENT,a.c/x,a.c/y,a.c/f@@\r\n,1,2,\r\n,3,4,\r\n" });
  await settle();
  const opts = h.sheets[0].opts;
  assert.match(opts.heading, /所有现有行.*缺用例无行位置/);
  assert.deepEqual(Array.from(opts.marks, m => [m.step, m.index, m.kind]),
    [[0, 0, "input"], [0, 1, "input"], [1, 0, "input"], [1, 1, "input"]]);
  assert.equal(opts.readOnly, true);
});

test("gap input drawer refuses different CSV headers instead of name matching", async () => {
  const h = harness(), value = job();
  value.attempts[0].csv_sha256 = "sha10";
  value.attempts[0].check = typedCheck();
  await ready(h, value);
  issueAction(h, "[data-gap-inputs]", { gapAttempt: "10", gapInputs: "0" });
  h.previews[0].resolve({ text: "mod,a.c/f,f,2,1\r\n#COMMENT,other.c/x,a.c/y,a.c/f@@\r\n,1,2,\r\n" });
  await settle();
  assert.equal(h.sheets.length, 0);
});

test("delayed diagnostic download stays tied to selected historical version and includes rows", async () => {
  const h = harness(), value = job(20);
  value.attempts[0].csv_sha256 = "historical";
  await ready(h, value);
  h.pin(10);
  issueAction(h, "[data-diagnostic]", { diagnostic: "10" });
  assert.equal(h.downloads.length, 0);
  assert.equal(h.diagnostics[0].attempt, 10);
  h.diagnostics[0].resolve(packetFor(value.attempts[0]));
  await settle();
  const packet = JSON.parse(await h.downloads[0].text());
  assert.equal(packet.candidate_sha256, "historical");
  assert.equal(packet.attempt_id, 10);
  assert.equal(packet.inputs.rows[0].inputs.I1, "1");
  assert.match(h.downloads[1].download, /1-10\.json$/);
  assert.equal(h.revisions.length, 0);
});

for (const change of ["selection", "close", "refresh", "wrong_hash", "wrong_check", "wrong_version"]) {
  test(`pending diagnostic cannot export after ${change}`, async () => {
    const h = harness(), value = job(20);
    value.attempts[0].csv_sha256 = "sha10";
    await ready(h, value);
    h.pin(10);
    issueAction(h, "[data-diagnostic]", { diagnostic: "10" });
    const packet = packetFor(value.attempts[0]);
    if (change === "selection") h.pin(20);
    if (change === "close") h.click("ut-d-close");
    if (change === "refresh") {
      h.click("ut-d-refresh"); await settle();
      h.details.at(-1).resolve(value); await settle();
    }
    if (change === "wrong_hash") packet.candidate_sha256 = "different";
    if (change === "wrong_check") packet.check = { issues: [{ code: "changed" }] };
    if (change === "wrong_version") packet.version.seq = 99;
    h.diagnostics[0].resolve(packet);
    await settle();
    assert.equal(h.downloads.length, 0);
    assert.equal(h.revisions.length, 0);
  });
}

test("a missing case names its configured requirement while missing annotations stay presentation work", async () => {
  const h = harness();
  const value = job();
  value.attempts[0].check.issues = [
    { code: "INPUT_OBLIGATION_UNRESOLVED", severity: "warning", detail: [{ obligation_id: "B:F", kind: "branch",
      gap_class: "MISSING_CASE", requirement: { configuration_status: "BOUND", missing_kind: "TEST_CASE",
        target: "判定 B；目标结果为假", requirements: [{ label: "C1 分支结果覆盖", required: true }] } }] },
    { code: "REFERENCE_ANNOTATION_MISSING", severity: "warning", message: "输入行仍存在，补注释即可",
      guidance: { title: "只缺参考注释", action_scope: "ANNOTATION", steps: ["恢复所列标签"], verification: "复查参考标签" },
      detail: [{ inputs: { x: "3" }, missing_kind: "ANNOTATION", annotation: "境界値 3" }] },
  ];
  await ready(h, value);
  const html = allIssues(h);
  assert.match(html, /配置依据[\s\S]*C1 分支结果覆盖=要求/);
  assert.match(html, /具体缺口[\s\S]*判定 B；目标结果为假/);
  assert.match(html, /需恢复的注释／标签[\s\S]*境界値 3/);
  assert.match(html, /处理入口[\s\S]*注释／标签/);
  assert.match(html, /输入行仍存在，补注释即可/);
});

test("issues are read one type at a time, repeated codes collapse and each type says who can fix it", async () => {
  const h = harness();
  const value = job();
  const review = { title: "主机用例集需人工复核", action_scope: "HUMAN_REVIEW", steps: ["核对证据"], verification: "复核" };
  value.attempts[0].check.issues = [
    { code: "INPUT_OBLIGATION_UNRESOLVED", severity: "warning", message: "3 个测试要求未解决",
      detail: [{ obligation_id: "B1", gap_class: "SEMANTIC_EVIDENCE" }, { obligation_id: "B2", gap_class: "MISSING_CASE" },
        { obligation_id: "B3", gap_class: "SEMANTIC_EVIDENCE" }] },
    { code: "HOST_SUITE_REVIEW_REQUIRED", severity: "warning", message: "B1 需复核", repairable: false, guidance: review },
    { code: "HOST_SUITE_REVIEW_REQUIRED", severity: "warning", message: "B3 需复核", repairable: false, guidance: review },
    { code: "ORACLE_MISMATCH", severity: "error", message: "期望值不一致", repairable: true },
  ];
  await ready(h, value);
  // Errors come first and are the default tab; only that type is rendered.
  let html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /class="sev-error on" aria-pressed="true" data-issue-tab="ORACLE_MISMATCH"/);
  assert.ok(html.indexOf('data-issue-tab="ORACLE_MISMATCH"') < html.indexOf('data-issue-tab="INPUT_OBLIGATION_UNRESOLVED"'));
  assert.match(html, /data-issue-tab="HOST_SUITE_REVIEW_REQUIRED"[^>]*>.*主机用例集需人工复核 <span class="ut-tab-n">2</);
  assert.match(html, /ut-issue-route is-model">可由本地模型补输入行/);
  assert.doesNotMatch(html, /B1 需复核/);

  html = issueTab(h, "HOST_SUITE_REVIEW_REQUIRED", 10);
  assert.equal((html.match(/<li class="ut-issue /g) || []).length, 1);
  assert.match(html, /<span class="ut-issue-count">×2<\/span>/);
  assert.equal((html.match(/处理入口/g) || []).length, 1);
  assert.match(html, /<ol class="ut-issue-msgs"><li>B1 需复核<\/li><li>B3 需复核<\/li><\/ol>/);
  assert.match(html, /ut-issue-route is-human">本地模型不能修正：需人工确认/);
  assert.match(html, /完整原始诊断[\s\S]*B1 需复核[\s\S]*B3 需复核/);

  html = issueTab(h, "INPUT_OBLIGATION_UNRESOLVED", 10);
  assert.match(html, /1 项缺用例可由本地模型补输入行，其余需补证据或人工确认/);
  // Gap cards fold to one line each.
  assert.equal((html.match(/<details class="ut-gap-card" data-keep="gap:10:B\d"><summary>/g) || []).length, 3);
  assert.equal(h.get("ut-d-issues-sum").textContent, "· 1 个错误 · 3 个警告");
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
  h.click("ut-d-edit-sheet");
  h.click("ut-d-import");
  await settle();
  assert.equal(h.reviews.length, 0);
  assert.equal(h.revisions.length, 0);
  assert.equal(h.sheets.length, 0);
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

test("dashboard splits the gates by whose turn it is", () => {
  const h = harness();
  const job = (state) => ({ in_index: true, latest_job: { state } });
  const { review } = h.common.dashboardSnapshot([
    job("awaiting_review"), job("awaiting_review"), job("needs_human"), job("blocked"), job("error"),
  ]);
  assert.deepEqual({ ...review }, {
    pending: 5, awaiting: 2, needs_human: 1, trouble: 2, approved: 0, rejected: 0 });
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
  assert.match(html, /Clang\/LLVM/);
  assert.match(html, /lm-cell-time"><code>26\/10\/19/);
  const queued = window.LMTaskRow.rowHtml({ ...task, finished_at: null }, { projectId: 4 });
  assert.match(queued, /lm-cell-time"><span class="muted">—<\/span>/);
  assert.doesNotMatch(queued, /<code><\/code>/);
  const changed = window.LMUt.workspaceTask({ ...raw, current_attempt_id: 20 });
  assert.notEqual(window.LMTaskRow.signature(task), window.LMTaskRow.signature(changed));
});

for (const [page, lists, counter] of [
  ["ut_tasks.js", { jobs: [{ ...job(10, "executing") }] }, "jobs"],
  ["ut_pipeline.js", { targets: [{ id: 1, in_index: true, row_no: 1, function: "f",
    source_rel: "src/a.c", latest_job: { id: 1, state: "executing" } }] }, "targets"],
]) {
  test(`${page} stops polling in a hidden tab and catches up when shown again`, async () => {
    const h = harness({ edit: true, review: true }, page, lists);
    await settle();
    const before = h.calls[counter];
    assert.ok(before >= 1 && h.timers.length >= 1, "active work schedules a poll");
    h.document.hidden = true;
    const scheduled = h.timers.length;
    h.timers[h.timers.length - 1]();
    await settle();
    assert.equal(h.calls[counter], before, "a hidden tab does not hit the server");
    assert.equal(h.timers.length, scheduled, "and does not reschedule");
    h.docListeners.visibilitychange();
    await settle();
    assert.equal(h.calls[counter], before, "still hidden: nothing to catch up");
    h.document.hidden = false;
    h.docListeners.visibilitychange();
    await settle();
    assert.equal(h.calls[counter], before + 1, "shown again: one immediate refresh");
    assert.ok(h.timers.length > scheduled, "which resumes polling");
  });
}

test("a stop request reads as 停止中 and only gates or stopped jobs accept actions", () => {
  const { common } = harness();
  assert.equal(common.jobState({ state: "executing", stop_requested: true }), "stopping");
  assert.equal(common.jobState({ state: "needs_human", stop_requested: true }), "needs_human");
  assert.equal(common.actionable({ state: "stopped" }), true);
  assert.equal(common.actionable({ state: "needs_human", superseded_by: 9 }), false);
  assert.equal(common.actionable({ state: "executing" }), false);
});

function listed(id, state, extra = {}) {
  return { id, batch_id: 1, state, current_attempt_id: 10, repair_rounds: 0, max_repair_rounds: 5,
    target: { function: "f" + id, source_rel: "src/a.c", source_name: "a.c" }, ...extra };
}

function requeueRow(h, target) {
  return h.get("ut-rows").listeners.click({
    target: { closest: (selector) => selector === "[data-requeue]"
      ? { dataset: { requeue: String(target) } } : null },
  });
}

test("the task list requeues an indexed inactive function as a new batch", async () => {
  const value = listed(1, "error", { target: { id: 7, in_index: true, function: "f" } });
  const h = harness(undefined, "ut_tasks.js", { jobs: [value] });
  await settle();
  assert.match(h.get("ut-rows").innerHTML, /data-requeue="7"/);
  assert.match(h.get("ut-rows").innerHTML, /data-retry="1"/);
  requeueRow(h, 7);
  await settle();
  assert.equal(h.batches.length, 1);
  assert.equal(JSON.stringify(h.batches[0].args), '[4,[7],""]');
  h.batches[0].resolve({ id: 20 });
  await settle();
  assert.match(h.get("ut-status").textContent, /批次 #20 已入队/);
  assert.equal(h.calls.jobs, 2);
});

test("job details can requeue without returning to the function index", async () => {
  const h = harness();
  await ready(h, { ...job(), target: { id: 8, in_index: true, function: "f" } });
  assert.equal(h.get("ut-d-requeue").hidden, false);
  h.click("ut-d-requeue");
  await settle();
  assert.equal(JSON.stringify(h.batches[0].args), '[4,[8],""]');
  assert.equal(h.get("ut-d-requeue").disabled, true);
  h.batches[0].resolve({ id: 21 });
  await settle();
  h.details.at(-1).resolve({ ...job(), superseded_by: 2,
    target: { id: 8, in_index: true, function: "f" } });
  await settle();
  assert.equal(h.get("ut-d-requeue").hidden, true);
});

for (const [name, state, extra, capabilities] of [
  ["active", "executing", {}, undefined],
  ["superseded", "error", { superseded_by: 9 }, undefined],
  ["removed", "error", { target: { id: 7, in_index: false } }, undefined],
  ["viewer", "error", {}, { edit: false, review: true }],
]) {
  test(`${name} jobs do not offer or submit requeue`, async () => {
    const value = { ...job(10, state), target: { id: 7, in_index: true }, ...extra };
    const h = harness(capabilities, "ut_tasks.js", { jobs: [value] });
    await ready(h, value);
    assert.doesNotMatch(h.get("ut-rows").innerHTML, /data-requeue="/);
    assert.equal(h.get("ut-d-requeue").hidden, true);
    requeueRow(h, 7);
    h.click("ut-d-requeue");
    await settle();
    assert.equal(h.batches.length, 0);
  });
}

test("bulk requeue selects eligible functions only and deduplicates targets", async () => {
  const h = harness(undefined, "ut_tasks.js", { jobs: [
    listed(1, "error", { target: { id: 7, in_index: true } }),
    listed(2, "approved", { target: { id: 8, in_index: true } }),
    listed(3, "queued", { target: { id: 9, in_index: true } }),
  ] });
  await settle();
  const all = h.get("ut-requeue-all");
  all.checked = true;
  all.listeners.change.call(all);
  assert.equal(h.get("ut-requeue-label").textContent, "重新入队（2）");
  h.click("ut-requeue");
  await settle();
  assert.equal(JSON.stringify(h.batches[0].args), '[4,[7,8],""]');
  h.batches[0].resolve({ id: 22 });
  await settle();
  assert.equal(h.get("ut-requeue").disabled, true);
});

test("cancelled or concurrent confirmations never submit duplicate batches", async () => {
  const confirmation = deferred();
  let confirmations = 0;
  const h = harness(undefined, "ut_tasks.js", {
    jobs: [listed(1, "error", { target: { id: 7, in_index: true } })],
    confirm: () => { confirmations += 1; return confirmation.promise; },
  });
  await settle();
  requeueRow(h, 7);
  requeueRow(h, 7);
  await settle();
  assert.equal(confirmations, 1);
  confirmation.resolve(false);
  await settle();
  assert.equal(h.batches.length, 0);
  requeueRow(h, 7);
  await settle();
  assert.equal(confirmations, 2);
});

test("a rejected requeue keeps selection and shows the server error", async () => {
  const h = harness(undefined, "ut_tasks.js", {
    jobs: [listed(1, "error", { target: { id: 7, in_index: true } })],
  });
  await settle();
  h.get("ut-rows").listeners.change({ target: { dataset: { requeueTarget: "7" }, checked: true } });
  h.click("ut-requeue");
  await settle();
  h.batches[0].reject(new Error("函数已在队列中"));
  await settle();
  assert.match(h.get("ut-status").textContent, /函数已在队列中/);
  assert.equal(h.get("ut-requeue").disabled, false);
  assert.equal(h.get("ut-requeue-label").textContent, "重新入队（1）");
});

test("refresh removes selections that are no longer eligible", async () => {
  const lists = { jobs: [listed(1, "error", { target: { id: 7, in_index: true } })] };
  const h = harness(undefined, "ut_tasks.js", lists);
  await settle();
  h.get("ut-rows").listeners.change({ target: { dataset: { requeueTarget: "7" }, checked: true } });
  assert.equal(h.get("ut-requeue").disabled, false);
  lists.jobs[0].state = "generating";
  h.click("ut-refresh");
  await settle();
  assert.equal(h.get("ut-requeue").disabled, true);
  h.click("ut-requeue");
  await settle();
  assert.equal(h.batches.length, 0);
});

test("superseded jobs leave the working list until asked for", async () => {
  const h = harness(undefined, "ut_tasks.js", { jobs: [
    listed(3, "awaiting_review"), listed(2, "stopped"), listed(1, "needs_human", { superseded_by: 3 }),
  ] });
  await settle();
  let rows = h.get("ut-rows").innerHTML;
  assert.doesNotMatch(rows, /"ut-num">#1</);
  assert.equal(h.get("ut-old-wrap").hidden, false);
  assert.equal(h.get("ut-old-n").textContent, 1);
  // A stopped job can be resumed from the list.
  assert.match(rows, /title="继续"[^>]*data-retry="2"/);

  const toggle = h.get("ut-show-old");
  toggle.checked = true;
  toggle.listeners.change.call(toggle);
  rows = h.get("ut-rows").innerHTML;
  assert.match(rows, /<tr class="is-stale"><td class="ut-check">[\s\S]*?<\/td><td class="ut-num">#1</);
  assert.match(rows, /已被 #3 取代/);
  assert.doesNotMatch(rows, /data-retry="1"/);
});

test("the job list shows host C0/C1/MC/DC on one line without a report link", async () => {
  const h = harness(undefined, "ut_tasks.js", { jobs: [
    listed(7, "awaiting_review", { coverage: { evidence_source: "HOST_EXECUTION", c0: 100, c1: 75, mcdc: null,
      has_report: true } }),
    listed(6, "awaiting_review", { coverage: null }),
  ] });
  await settle();
  const rows = h.get("ut-rows").innerHTML;
  assert.match(rows, /class="is-full"><b>C0<\/b> 100%[\s\S]*class="is-gap"><b>C1<\/b> 75%[\s\S]*class="is-missing"><b>MC\/DC<\/b> —/);
  assert.match(rows, /class="ut-cov-mini is-inline"/);
  assert.doesNotMatch(rows, /逐行报告|data-coverage/);
  assert.match(rows, /#6[\s\S]*<td><span class="submuted">—<\/span><\/td>/);
});

test("an active job offers 停止 until its stop is requested", async () => {
  const h = harness(undefined, "ut_tasks.js", { jobs: [
    listed(5, "executing"), listed(4, "repairing", { stop_requested: true }),
  ] });
  await settle();
  const rows = h.get("ut-rows").innerHTML;
  assert.match(rows, /data-stop="5"/);
  assert.doesNotMatch(rows, /data-stop="4"/);
  assert.match(rows, /停止中/);
});

test("a superseded job opens read-only with a link to its replacement", async () => {
  const h = harness();
  await ready(h, { ...job(10, "needs_human"), superseded_by: 7 });
  assert.equal(h.get("ut-d-superseded").hidden, false);
  assert.match(h.get("ut-d-superseded").innerHTML, /data-open-job="7"/);
  assert.equal(h.get("ut-d-gate-forms").hidden, true);
  assert.equal(h.get("ut-d-retry").hidden, true);
  assert.match(h.get("ut-d-progress-summary").textContent, /历史作业，已被 #7 取代/);
  assert.doesNotMatch(h.get("ut-d-progress-steps").innerHTML, /aria-current/);
});

test("a stopped job can be resumed, revised or reviewed; a running one can be stopped", async () => {
  const h = harness();
  await ready(h, job(10, "stopped"));
  assert.equal(h.get("ut-d-gate-forms").hidden, false);
  assert.equal(h.get("ut-d-retry").hidden, false);
  assert.equal(h.get("ut-d-retry-label").textContent, "继续");
  assert.equal(h.get("ut-d-stop").hidden, true);
  assert.match(h.get("ut-d-gate-hint").textContent, /已停止/);
  assert.doesNotMatch(h.get("ut-d-progress-steps").innerHTML, /aria-current/);
  assert.equal(h.get("ut-d-review-section").hidden, false);

  h.click("ut-d-refresh");
  await settle();
  h.details.at(-1).resolve(job(10, "executing"));
  await settle();
  assert.equal(h.get("ut-d-stop").hidden, false);
  h.click("ut-d-stop");
  await settle();
  assert.deepEqual(h.stops.map((s) => s.id), [1]);
});

test("a gate code names the category and the next step", async () => {
  const h = harness();
  await ready(h, { ...job(10, "needs_human"), gate_code: "MODEL_STALLED", reason: "本地模型本轮没有提出任何新增行" });
  assert.equal(h.get("ut-d-gate-code").hidden, false);
  assert.equal(h.get("ut-d-gate-code").textContent, "模型无进展");
  assert.match(h.get("ut-d-gate-hint").textContent, /重复调用不会有不同结果[\s\S]*审核的是当前版本/);
  // Every server-side code has a hint.
  const codes = ["MACHINE_PASS", "OPEN_GAPS_EXHAUSTED", "ROUNDS_EXHAUSTED", "NOT_ROW_FIXABLE",
    "HUMAN_REVISION_FAILED", "CANDIDATE_REJECTED", "MODEL_STALLED", "CHECK_BLOCKED",
    "CONFIGURATION_CONFLICT", "REPORT_INCOMPLETE", "INTEGRITY_ERROR", "UNKNOWN_VERDICT",
    "NO_TESTCSV", "PROMPT_PREFIX_CHANGED", "PROMPT_BUDGET_EXCEEDED", "ENV_BLOCKED", "GENERATION_FAILED",
    "TOOL_ERROR", "MODEL_UNAVAILABLE", "INTERNAL_ERROR", "INTERRUPTED", "STOPPED"];
  const source = fs.readFileSync(path.resolve(__dirname, "../../app/models/ut_pipeline.py"), "utf8");
  const declared = [...source.matchAll(/^    ([A-Z_]+) = "\1"/gm)].map((m) => m[1]);
  assert.deepEqual(declared.sort(), [...codes].sort());
  for (const code of codes) assert.ok(h.common.GATE[code], code);
});

test("consecutive model rounds without a candidate fold into one row", async () => {
  const h = harness();
  const round = (id, seq, reviser) => ({ id, seq, origin: "LLM", has_csv: false, verdict: "INVALID",
    check: null, reviser, created_at: "2026-10-20T10:00:00Z" });
  const value = { ...job(10, "needs_human"), attempts: [
    candidate(10, 1),
    round(11, 2, { rationale: "", rows: [], rejected: [] }),
    round(12, 3, { rationale: "", rows: [], rejected: [{ error: "x" }] }),
    round(13, 4, { rationale: "", rows: [], rejected: [] }),
  ] };
  await ready(h, value);
  let html = h.get("ut-d-attempts").innerHTML;
  assert.match(html, /本地模型 3 轮未产出候选[\s\S]*无进展 2 · 行被拒 1/);
  assert.match(html, /data-attempt="11" data-run-member="1:2" hidden/);
  assert.match(html, /<tr data-attempt="10" class="is-current"/);

  h.get("ut-d-attempts").listeners.click({
    target: { closest: (sel) => sel === "[data-run-toggle]" ? { dataset: { runToggle: "1:2" } } : null },
  });
  html = h.get("ut-d-attempts").innerHTML;
  assert.doesNotMatch(html, /data-run-member="1:2" hidden/);
  assert.match(html, />收起</);
});

test("实施一览 opens a job from the shared hover strip", async () => {
  const h = harness(undefined, "ut_pipeline.js", { targets: [
    { id: 1, in_index: true, row_no: 1, function: "f", source_rel: "src/a.c", source_name: "a.c",
      latest_job: { id: 30, state: "awaiting_review", repair_rounds: 0 } },
    { id: 2, in_index: true, row_no: 2, function: "g", source_rel: "src/a.c", source_name: "a.c",
      latest_job: { id: 31, state: "approved", repair_rounds: 0 } },
    { id: 3, in_index: true, row_no: 3, function: "h", source_rel: "src/a.c", source_name: "a.c" },
  ] });
  await settle();
  const html = h.get("ut-rows").innerHTML;
  assert.match(html, /<td class="lm-row-actions"><span class="row-acts"><a class="btn small btn-icon" href="\/lanmatrix\/projects\/4\/tasks\?job=30" title="审核作业 #30"/);
  assert.match(html, /title="查看作业 #31"/);
  assert.match(html, /<td class="lm-row-actions"><span class="row-acts"><\/span><\/td><\/tr>$/);
  assert.doesNotMatch(html, /btn small ghost/);
});



test("job detail identifies the batch by its test rules and lists policy requirements separately", async () => {
  const h = harness();
  await ready(h, { ...job(), batch: { provenance: {
    test_rules_ref: "universal-test-rules@1", generator_commit: "0123456789abcdef", baseline_ref: "psd-rebuild@1.1",
  }, constraints: {
    effective_test_policy: { version: "1", boundary_adjacent: true, c1: true, mcdc: true, arithmetic: false, combination_group: true },
    coverage_policy: { c0: true }, test_rules_ref: "universal-test-rules@1", test_policy_sha256: "d".repeat(64),
  } } });
  assert.equal(h.get("ut-d-batch").textContent, "#1 · universal-test-rules@1 · 01234567");
  // Legacy Baseline provenance from batches created before its removal is no longer displayed.
  assert.doesNotMatch(h.get("ut-d-batch").textContent, /psd-rebuild/);
  assert.match(h.get("ut-d-requirements").textContent, /测试要求 v1/);
  assert.match(h.get("ut-d-requirements").textContent, /C0 · C1 · MC\/DC · 边界值 ±1 · MC\/DC「組合せ」分组/);
  assert.match(h.get("ut-d-requirements").textContent, /universal-test-rules@1/);
});
