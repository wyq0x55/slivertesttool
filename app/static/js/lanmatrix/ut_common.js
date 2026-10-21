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
  var ORIGIN = { GENERATOR: "生成器", LLM: "本地模型", CAPTURE: "执行捕获", HUMAN: "人工" };
  var ACTIVE = { queued: 1, generating: 1, executing: 1, repairing: 1 };
  /* Machine outcomes that wait for a person. */
  var GATES = { awaiting_review: 1, needs_human: 1, blocked: 1, error: 1 };

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
  function verdictPill(verdict) {
    if (!verdict) return '<span class="muted">—</span>';
    var m = VERDICT[verdict] || ["pending", verdict];
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
      sil_name: "WinAMS", submitter: job.submitter || "",
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
        : job.last_verdict ? verdictPill(job.last_verdict)
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
      review: { pending: pending, approved: states.approved || 0, rejected: states.rejected || 0 },
    };
  }

  window.LMUt = {
    STATE: STATE, VERDICT: VERDICT, ORIGIN: ORIGIN, ACTIVE: ACTIVE, GATES: GATES,
    FILTERS: FILTERS, jobState: jobState, actionable: actionable, esc: esc, statePill: statePill, applyState: applyState,
    verdictPill: verdictPill, time: time, say: say, can: can, selectFilter: selectFilter,
    dashboardSnapshot: dashboardSnapshot, workspaceTask: workspaceTask, workspaceCells: workspaceCells
  };
})(window, document);
