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
  const stops = [];
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
      utAttemptUrl: (pid, job, attempt) => `/csv/${job}/${attempt}`,
      utArtifactUrl: (pid, job, attempt, artifact) => `/artifact/${job}/${attempt}/${artifact}`,
      utExportUrl: (pid, batch) => `/export/${batch || "all"}`,
      utReview(...args) { const d = deferred(); reviews.push({ ...d, args }); return d.promise; },
      utRevise(...args) { const d = deferred(); revisions.push({ ...d, args }); return d.promise; },
      utStop(pid, id) { const d = deferred(); stops.push({ ...d, id }); return d.promise; },
    },
    LMUI: { alert: async () => {} },
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
    document, docListeners, timers, calls, downloads };
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
  const html = h.get("ut-d-issues").innerHTML;
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
  assert.match(html, /完整原始诊断[\s\S]*psd\.4\.compare[\s\S]*intent_status/);
  assert.match(html, /本报告未提供处理指引/);
  assert.equal(h.get("ut-d-issues-sum").textContent, "· 1 个警告");
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
  const html = h.get("ut-d-issues").innerHTML;
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
  const html = h.get("ut-d-issues").innerHTML;
  assert.match(html, /配置依据[\s\S]*C1 分支结果覆盖=要求/);
  assert.match(html, /具体缺口[\s\S]*判定 B；目标结果为假/);
  assert.match(html, /需恢复的注释／标签[\s\S]*境界値 3/);
  assert.match(html, /处理入口[\s\S]*注释／标签/);
  assert.match(html, /输入行仍存在，补注释即可/);
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
  assert.match(html, /WinAMS/);
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
  assert.match(rows, /<tr class="is-stale"><td class="ut-num">#1</);
  assert.match(rows, /已被 #3 取代/);
  assert.doesNotMatch(rows, /data-retry="1"/);
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
    "HUMAN_REVISION_FAILED", "CANDIDATE_REJECTED", "MODEL_STALLED", "CHECK_BLOCKED", "UNKNOWN_VERDICT",
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



test("job detail displays explicit policy requirements without replacing legacy baseline text", async () => {
  const h = harness();
  await ready(h, { ...job(), batch: { provenance: { baseline_ref: "psd-rebuild@1.1" }, constraints: {
    effective_test_policy: { version: "1", boundary_adjacent: true, c1: true, mcdc: true, arithmetic: false, combination_group: true },
    coverage_policy: { c0: true }, test_rules_ref: "universal-test-rules@1", test_policy_sha256: "d".repeat(64),
  } } });
  assert.match(h.get("ut-d-batch").textContent, /psd-rebuild@1\.1/);
  assert.match(h.get("ut-d-requirements").textContent, /测试要求 v1/);
  assert.match(h.get("ut-d-requirements").textContent, /C0 · C1 · MC\/DC · 边界值 ±1 · MC\/DC「組合せ」分组/);
  assert.match(h.get("ut-d-requirements").textContent, /universal-test-rules@1/);
});
