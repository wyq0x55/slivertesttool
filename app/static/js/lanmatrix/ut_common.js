/*
 * ut_common.js — window.LMUt: the unit-test project vocabulary.
 *
 * Unit-test pages render through the same badge (LMPill) and time format
 * (LMTaskRow.fmtTime) as the Silver pages; only the state names differ. A UT
 * job carries two independent facts, kept in separate columns so that a
 * human approval of a machine FAIL reads as exactly that:
 *   state   — where the job is in the pipeline / review gate,
 *   verdict — what the checker said about the current candidate.
 */
(function (window, document) {
  "use strict";

  /* state -> [pill class, label]. Classes are the shared st-* palette. */
  var STATE = {
    queued: ["queued", "排队中"],
    generating: ["running", "生成中"],
    executing: ["running", "执行中"],
    repairing: ["running", "模型修正中"],
    awaiting_review: ["pending", "待审核"],
    needs_human: ["error", "需人工"],
    blocked: ["error", "环境阻塞"],
    error: ["failed", "异常"],
    approved: ["passed", "审核通过"],
    rejected: ["cancelled", "审核驳回"],
    stopped: ["cancelled", "已停止"],
    /* Display-only: an active job whose stop request the worker has not
       reached yet (it finishes the running WinAMS / model call first). */
    stopping: ["running", "停止中"]
  };
  var VERDICT = {
    PASS: ["passed", "PASS"], FORMAT_OK: ["pending", "仅格式通过"],
    FAIL: ["failed", "FAIL"], BLOCKED: ["error", "阻塞"], INVALID: ["failed", "无效"]
  };
  var ORIGIN = { GENERATOR: "生成器", LLM: "本地模型", CAPTURE: "执行捕获", HUMAN: "人工", HOST_DERIVED: "类型推导" };
  var ACTIVE = { queued: 1, generating: 1, executing: 1, repairing: 1 };
  /* Machine outcomes that wait for a person. */
  var GATES = { awaiting_review: 1, needs_human: 1, blocked: 1, error: 1 };

  /* Gate code (models.ut_pipeline.GateCode) -> [label, what to do next].
   * The reason text carries the specifics; this says which action fits. */
  var RETRY = "点「重试」继续。";
  var REQUEUE = "处理后从「实施一览」重新入队。";
  var REVISE = "请人工修订，或在审核意见中说明后通过 / 驳回。";
  var GATE = {
    MACHINE_PASS: ["机器通过", "机器校验通过。核对版本历史与校验问题后决定通过或驳回。"],
    OPEN_GAPS_EXHAUSTED: ["仍有缺口", "机器校验通过，但仍有输入义务未证明，模型修订已到上限。" + REVISE],
    ROUNDS_EXHAUSTED: ["修订已到上限", "本地模型已用完修订轮数。" + REVISE],
    NOT_ROW_FIXABLE: ["需改期望值或格式", "剩余问题不能靠新增输入行解决。" + REVISE],
    HUMAN_REVISION_FAILED: ["人工修订未通过", "人工修订仍未通过校验，不会交给模型。" + REVISE],
    CANDIDATE_REJECTED: ["新增行被拒", "模型新增的行在执行前被检查拒绝，当前版本未变。" + REVISE],
    MODEL_STALLED: ["模型无进展", "模型本轮没有提出任何新增行，输入未变，重复调用不会有不同结果。" + REVISE],
    CHECK_BLOCKED: ["不可自动修复", "校验发现无法通过修订解决的问题。" + REVISE],
    CONFIGURATION_CONFLICT: ["测试配置需复核", "义务与冻结测试要求冲突，不能自动补行。请核对版本并重新生成或校验；通过须填写审核意见。"],
    REPORT_INCOMPLETE: ["报告明细不完整", "请重新生成或校验取得完整义务清单；当前预览不能代表全部完成，通过须填写审核意见。"],
    INTEGRITY_ERROR: ["候选证据完整性异常", "候选 CSV、校验或执行摘要不一致，已停止自动修复。请恢复同版文件并重新校验；通过须填写审核意见。"],
    UNKNOWN_VERDICT: ["未知判定", "ut-agent 返回了平台不认识的判定，请确认 ut-agent 版本。"],
    NO_TESTCSV: ["未生成 CSV", "生成器没有产出 TestCsv，无法执行。" + REQUEUE],
    PROMPT_PREFIX_CHANGED: ["证据已变化", "基准、列或语义证据与本作业的固定提示词不一致，未调用模型。" + REQUEUE],
    PROMPT_BUDGET_EXCEEDED: ["上下文超限", "修订上下文超出预算，未调用模型，不计修订次数。" + REVISE],
    ENV_BLOCKED: ["环境阻塞", "WinAMS 执行环境不完整。修好后" + RETRY],
    GENERATION_FAILED: ["生成失败", "ut-agent 生成失败。" + REQUEUE],
    TOOL_ERROR: ["工具异常", "ut-agent 调用失败。确认 ut-agent 正常后" + RETRY],
    MODEL_UNAVAILABLE: ["模型不可用", "本地模型没有响应，本轮不计修订次数。服务恢复后" + RETRY + "也可以直接人工修订。"],
    INTERNAL_ERROR: ["内部错误", "平台内部错误。" + RETRY],
    INTERRUPTED: ["运行中断", "worker 在运行中被中断，当前版本与证据已保留。确认 worker 正常后" + RETRY],
    STOPPED: ["已停止", "作业已停止，当前版本和机器证据都已保留：可以继续运行、人工修订，或直接审核。"]
  };
  function gateInfo(job) { return (job && GATE[job.gate_code]) || null; }

  /* The state to display: a pending stop request reads as 停止中. */
  function jobState(job) {
    return job.stop_requested && ACTIVE[job.state] ? "stopping" : job.state;
  }
  /* Accepts revision / review / retry: a gate or a stopped job, unless the
     function was queued again (the server refuses superseded jobs). */
  function actionable(job) {
    return !!(job && (GATES[job.state] || job.state === "stopped") && !job.superseded_by);
  }

  /* Segmented filters shared by 实施一览 and 任务与运行. `test` sees a state
   * (or null for a target that was never queued). */
  var FILTERS = {
    "": function () { return true; },
    none: function (s) { return !s; },
    active: function (s) { return !!ACTIVE[s]; },
    awaiting_review: function (s) { return s === "awaiting_review"; },
    needs_human: function (s) { return s === "needs_human"; },
    trouble: function (s) { return s === "blocked" || s === "error"; },
    stopped: function (s) { return s === "stopped"; },
    approved: function (s) { return s === "approved"; },
    rejected: function (s) { return s === "rejected"; }
  };

  var esc = LMPill.esc;

  function statePill(state, tip) {
    if (!state) return LMPill.html("notask", "未执行");
    var m = STATE[state] || ["notask", state];
    return LMPill.html(m[0], m[1], tip);
  }
  function applyState(el, state) {
    var m = STATE[state] || ["notask", state || "未执行"];
    LMPill.apply(el, m[0], m[1]);
  }
  function verdictPill(verdict, backend) {
    if (!verdict) return '<span class="muted">—</span>';
    var m = VERDICT[verdict] || ["pending", verdict];
    if (backend === "clang") return LMPill.html(m[0], "主机 " + verdict,
      "仅主机观测比较；实测覆盖率不代表基准覆盖率验收，不计入目标证据或正式交付验收");
    return LMPill.html(m[0], m[1], "机器判定：" + verdict);
  }
  function time(iso) {
    return window.LMTaskRow ? LMTaskRow.fmtTime(iso) : (iso || "");
  }
  /* Inline status line under the page head (#ut-status). */
  function say(msg, isError) {
    ["ut-status", "ut-detail-status"].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = msg || "";
      el.classList.toggle("is-error", !!isError);
    });
  }
  function can(key) {
    return !!(window.LM && LM.projectCapabilities && LM.projectCapabilities[key]);
  }
  function selectFilter(container, value) {
    container.querySelectorAll("button").forEach(function (button) {
      var selected = button.dataset.filter === value;
      button.classList.toggle("on", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }
  function workspaceTask(job) {
    // View-only adapter. The namespaced key is never sent to Silver endpoints.
    return {
      kind: "ut_job", task_id: "ut:" + job.id, project_id: job.project_id,
      test_id: (job.target || {}).function || ("作业 #" + job.id),
      sil_name: job.execution_backend === "clang" ? "Clang 主机（实验）" : "WinAMS", submitter: job.submitter || "",
      status: job.task_status, created_at: job.created_at, finished_at: job.updated_at,
      review: {
        status: job.state === "approved" || job.state === "rejected" ? job.state
          : GATES[job.state] ? "pending" : "",
        note: job.review_note || "", reviewer_name: job.reviewer_name || "",
      },
      utJob: job,
    };
  }
  function workspaceCells(task) {
    var job = task.utJob;
    var decided = job.state === "approved" || job.state === "rejected";
    return {
      status: ACTIVE[job.state] ? statePill(job.state)
        : job.last_verdict ? verdictPill(job.last_verdict, job.execution_backend)
          : job.generation_status === "ERROR" ? LMPill.html("error", "生成异常")
            : LMPill.html("notask", "未判定"),
      // Decided and awaiting jobs read with the run list's review vocabulary
      // (待审核 / 已通过 / 已驳回); the other gates name the workflow problem.
      review: decided || job.state === "awaiting_review" ? window.LMTaskRow.reviewBadge(task)
        : GATES[job.state] ? LMPill.html("pending", STATE[job.state][1], job.reason)
          : '<span class="muted">—</span>',
      progress: '<span class="muted" title="模型修正轮数">修正 ' + esc(job.repair_rounds) +
        "/" + esc(job.max_repair_rounds) + "</span>",
    };
  }
  function dashboardSnapshot(targets) {
    var current = targets.filter(function (target) { return target.in_index; });
    var states = {};
    var passed = 0;
    current.forEach(function (target) {
      var job = target.latest_job;
      var state = job ? job.state : "none";
      states[state] = (states[state] || 0) + 1;
      if (job && job.last_verdict === "PASS") ++passed;
    });
    var total = current.length;
    var unrun = states.none || 0;
    var pending = Object.keys(GATES).reduce(function (sum, state) {
      return sum + (states[state] || 0);
    }, 0);
    function percent(value) { return total ? Math.round(100 * value / total) : 0; }
    return {
      ut_states: states,
      summary: {
        total: total, planned: total, executed: total - unrun, not_run: unrun,
        passed: passed, failed: states.needs_human || 0,
        errored: (states.blocked || 0) + (states.error || 0),
        exempt_approved: states.approved || 0,
        executed_pct: percent(total - unrun), passed_pct: percent(passed),
      },
      // `pending` is every gate; the parts say whose turn it is: a reviewer's
      // (awaiting), an editor's (needs_human) or the environment's (trouble).
      review: {
        pending: pending, awaiting: states.awaiting_review || 0,
        needs_human: states.needs_human || 0,
        trouble: (states.blocked || 0) + (states.error || 0),
        approved: states.approved || 0, rejected: states.rejected || 0,
      },
    };
  }

  window.LMUt = {
    STATE: STATE, VERDICT: VERDICT, ORIGIN: ORIGIN, ACTIVE: ACTIVE, GATES: GATES,
    FILTERS: FILTERS, jobState: jobState, actionable: actionable, GATE: GATE, gateInfo: gateInfo, esc: esc, statePill: statePill, applyState: applyState,
    verdictPill: verdictPill, time: time, say: say, can: can, selectFilter: selectFilter,
    dashboardSnapshot: dashboardSnapshot, workspaceTask: workspaceTask, workspaceCells: workspaceCells
  };
})(window, document);
