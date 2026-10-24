/*
 * 任务与运行 of a unit-test project: every ut-agent → WinAMS job, and the job
 * detail where its attempts are inspected and its human gate is worked.
 *
 * Mirrors project_tasks.js: the detail is an inline section toggled over the
 * list, and the open job lives in `?job=` so it can be linked and Back
 * returns to the list. Every machine outcome lands at a human gate
 * (awaiting_review, needs_human, blocked, error); nothing is approved here
 * without a person pressing 通过.
 *
 * A candidate CSV opens in the shared step-detail drawer (steps_editor.js) as
 * a table of comments and input / output values (ut_testcsv.js maps the
 * TestCsv onto it). The drawer is read-only for viewing; a human revision is
 * edited in the same drawer, or imported as a CSV file.
 */
(function (window, document) {
  "use strict";

  var root = document.querySelector(".lm-ut-tasks");
  if (!root) return;
  var pid = Number(root.dataset.project);
  var U = window.LMUt, esc = U.esc, say = U.say;
  var POLL_MS = 3000;
  var BATCH_ZH = { generating: "生成中", running: "执行中", done: "完成", failed: "失败" };
  // Same hover icon strip as the Silver task list (task_row.js).
  var ICO = (window.LMTaskRow && window.LMTaskRow.ICO) || {};
  var ICO_EDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/>' +
    '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  var STOP_ICO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>';

  var jobs = [];
  var filter = (window.LMUrl && LMUrl.get("filter")) || "";
  var query = (window.LMUrl && LMUrl.get("q")) || "";
  var batchId = (window.LMUrl && LMUrl.get("batch")) || "";
  var showOld = false;
  var batches = [];
  // Expanded runs of fruitless model rounds, keyed "<job>:<first seq>".
  var expandedRuns = {};
  var job = null;
  var openJobId = null;
  var shownAttempt = null;
  var timer = null;
  var detailEpoch = 0;
  var previewEpoch = 0;
  var diagnosticEpoch = 0;
  var gapPages = {};
  var listEpoch = 0;
  var detailRequest = 0;
  var detailFresh = false;
  var detailSyncedAt = null;
  var detailSyncFailed = false;
  var actionPending = false;

  function $(id) { return document.getElementById(id); }
  function iconButton(title, icon, data) {
    return '<button class="btn small btn-icon" type="button" title="' + title + '" aria-label="' +
      title + '" ' + data + ">" + (icon || esc(title)) + "</button>";
  }
  function iconLink(title, icon, href) {
    return '<a class="btn small btn-icon" href="' + esc(href) + '" title="' + title + '" aria-label="' +
      title + '">' + (icon || esc(title)) + "</a>";
  }

  // ------------------------------------------------------------------ list
  function visible(j) {
    if (j.superseded_by && !showOld) return false;
    if (!(U.FILTERS[filter] || U.FILTERS[""])(j.state)) return false;
    if (!query) return true;
    var t = j.target || {};
    return ((t.function || "") + " " + (t.source_rel || "")).toLowerCase()
      .indexOf(query.toLowerCase()) >= 0;
  }

  function renderRows() {
    var rows = jobs.filter(visible);
    var old = jobs.filter(function (j) { return j.superseded_by; }).length;
    // Export follows the batch filter; one function counts once.
    var approved = {};
    jobs.forEach(function (j) { if (j.state === "approved") approved[(j.target || {}).id || j.id] = 1; });
    var approvedCount = Object.keys(approved).length;
    $("ut-export").hidden = !approvedCount;
    $("ut-export").href = LMApi.utExportUrl(pid, batchId);
    $("ut-export-label").textContent = "导出已通过版本（" + approvedCount + "）";
    $("ut-old-wrap").hidden = !old;
    $("ut-old-n").textContent = old;
    $("ut-rows").innerHTML = rows.length ? rows.map(function (j) {
      var t = j.target || {};
      var resumable = j.state === "blocked" || j.state === "error" || j.state === "stopped";
      var retry = U.can("edit") && j.current_attempt_id && resumable && !j.superseded_by
        ? iconButton(j.state === "stopped" ? "继续" : "重试", ICO.retest, 'data-retry="' + j.id + '"') : "";
      var stop = U.can("edit") && U.ACTIVE[j.state] && !j.stop_requested
        ? iconButton("停止", STOP_ICO, 'data-stop="' + j.id + '"') : "";
      var download = j.current_attempt_id
        ? iconLink("下载当前版本", ICO.download, LMApi.utAttemptUrl(pid, j.id, j.current_attempt_id)) : "";
      return "<tr" + (j.superseded_by ? ' class="is-stale"' : "") + ">" +
        '<td class="ut-num">#' + j.id + "</td>" +
        '<td class="lm-cell-testid"><a href="?job=' + j.id + '" data-job="' + j.id + '"><code>' +
          esc(t.function || "") + "</code></a>" +
          '<div class="ut-src submuted" title="' + esc(t.source_rel || "") + '">' +
          esc(t.source_name || "") + " · 批次 #" + j.batch_id + "</div></td>" +
        "<td>" + U.statePill(U.jobState(j), rowTip(j)) +
          (j.superseded_by ? '<div class="submuted">已被 #' + j.superseded_by + " 取代</div>" : "") + "</td>" +
        "<td>" + U.verdictPill(j.last_verdict) + "</td>" +
        '<td class="ut-num">' + j.repair_rounds + " / " + j.max_repair_rounds + "</td>" +
        '<td class="lm-cell-time"><code>' + esc(U.time(j.updated_at)) + "</code></td>" +
        '<td class="lm-row-actions"><span class="row-acts">' +
        iconButton(U.actionable(j) ? "审核" : "查看", ICO.view, 'data-job="' + j.id + '"') +
        stop + retry + download + "</span></td></tr>";
    }).join("") : '<tr><td colspan="7" class="muted">' +
      (jobs.length ? "没有符合条件的作业" : "还没有作业：在「实施一览」勾选函数后加入队列") + "</td></tr>";
  }

  async function retryFromList(id, button) {
    if (!U.can("edit")) return;
    button.disabled = true;
    try {
      await LMApi.utRetry(pid, id);
      say("作业 #" + id + " 已重新开始");
    } catch (e) {
      say(e.message, true);
    }
    await refresh();
  }

  // Hovering a gate state says why the job stopped there.
  function rowTip(j) {
    var info = U.gateInfo(j);
    return info ? info[0] + (j.reason ? "：" + j.reason : "") : j.reason || undefined;
  }

  function stopNote(job) {
    return job.state === "queued" ? "已停止" : "将在当前 WinAMS / 模型调用结束后停止，已得到的结果会保留";
  }

  async function stopFromList(id, button) {
    if (!U.can("edit")) return;
    button.disabled = true;
    try {
      var stopped = await LMApi.utStop(pid, id);
      say("作业 #" + id + " " + stopNote(stopped));
    } catch (e) {
      say(e.message, true);
    }
    await refresh();
  }

  function selectedBatchActive() {
    var b = batches.find(function (item) { return String(item.id) === String(batchId); });
    if (!b) return 0;
    return Object.keys(b.states).reduce(function (sum, state) {
      return sum + (U.ACTIVE[state] ? b.states[state] : 0);
    }, 0);
  }

  async function loadBatches() {
    batches = await LMApi.utBatches(pid);
    var sel = $("ut-batch");
    sel.innerHTML = '<option value="">全部批次</option>' + batches.map(function (b) {
      var n = Object.keys(b.states).reduce(function (s, k) { return s + b.states[k]; }, 0);
      var prov = b.provenance || {};
      return '<option value="' + b.id + '">#' + b.id + " · " + esc(BATCH_ZH[b.status] || b.status) +
        " · " + n + " 个作业" + (prov.baseline_ref ? " · " + esc(prov.baseline_ref) : "") + "</option>";
    }).join("");
    sel.value = batchId;
    $("ut-batch-stop").hidden = !U.can("edit") || !selectedBatchActive();
    renderBatchInfo();
  }

  // The selected batch at a glance: where its jobs are and what produced them.
  function renderBatchInfo() {
    var box = $("ut-batch-info");
    var b = batches.find(function (item) { return String(item.id) === String(batchId); });
    box.hidden = !b;
    if (!b) { box.innerHTML = ""; return; }
    var prov = b.provenance || {};
    var states = Object.keys(b.states).sort(function (x, y) { return b.states[y] - b.states[x]; })
      .map(function (s) { return U.statePill(s) + " " + b.states[s]; }).join(" ");
    var facts = ["批次 #" + b.id + (b.label ? "「" + b.label + "」" : ""), BATCH_ZH[b.status] || b.status,
      prov.baseline_ref, prov.generator_commit ? "生成器 " + String(prov.generator_commit).slice(0, 8) : "",
      "创建 " + U.time(b.created_at), b.finished_at ? "结束 " + U.time(b.finished_at) : ""]
      .filter(Boolean).map(esc).join(" · ");
    box.innerHTML = '<span class="ut-batch-facts">' + facts + '</span><span class="ut-batch-states">' +
      states + "</span>" + (b.error ? '<span class="ut-batch-err">' + esc(b.error) + "</span>" : "");
  }

  async function loadJobs() {
    var epoch = ++listEpoch;
    var rows = await LMApi.utJobs(pid, batchId ? { batch_id: batchId } : {});
    if (epoch !== listEpoch) return;
    jobs = rows;
    renderRows();
  }

  function busy() {
    return jobs.some(function (j) { return U.ACTIVE[j.state]; }) || (job && U.ACTIVE[job.state]);
  }

  // A hidden tab skips its poll and stops rescheduling; becoming visible again
  // refreshes once, which resumes polling while work is still active.
  var pausedWhileHidden = false;
  function poll() {
    if (document.hidden) { pausedWhileHidden = true; renderSync(); return; }
    refresh();
  }
  document.addEventListener("visibilitychange", function () {
    renderSync();
    if (!document.hidden && pausedWhileHidden) { pausedWhileHidden = false; refresh(); }
  });

  async function refresh() {
    clearTimeout(timer);
    var id = openJobId, epoch = detailEpoch, request = detailRequest;
    try {
      await Promise.all([loadJobs(), loadBatches()]);
      if (openJobId) await loadJob(openJobId, detailEpoch);
    } catch (e) {
      if (id === openJobId && epoch === detailEpoch && request === detailRequest) staleDetail();
      say(e.message, true);
    }
    if (busy()) timer = setTimeout(poll, POLL_MS);
  }

  // ---------------------------------------------------------------- detail
  // Engine codes (COVERAGE_C0_INCOMPLETE) have no break opportunity of their
  // own; one per line, breakable after each underscore, so the column does not
  // push the table past a laptop-width panel.
  function wrapCode(text) { return esc(text).replace(/([_/])/g, "$1<wbr>"); }
  function topIssues(check) {
    if (!check) return "—";
    return (check.issues || []).filter(function (i) { return i.severity === "error"; })
      .slice(0, 3).map(function (i) { return "<div>" + wrapCode(i.code) + "</div>"; }).join("") || "—";
  }
  // One metric per line, each kept whole: joined into a sentence the column
  // squeezed "C0 100% / C1 100% / MCDC 100%" into a seven-line sliver.
  function coverageRequirements(constraints) {
    var policy = constraints && (constraints.effective_test_policy || constraints.test_policy);
    var required = constraints && constraints.coverage_policy;
    if (!policy && !required) return "";
    var items = [];
    if (policy) items.push("测试要求 v" + (policy.version || "1"), "最小值／最大值／边界值");
    [["c0", "C0"], ["c1", "C1"], ["mcdc", "MC/DC"]].forEach(function (item) {
      var enabled = policy ? item[0] === "c0" ? required && required.c0 : policy[item[0]] : required[item[0]];
      if (enabled) items.push(item[1]);
    });
    if (policy && policy.boundary_adjacent) items.push("边界值 ±1");
    if (policy && policy.arithmetic) items.push("演算式（含按位运算）");
    if (policy && policy.combination_group) items.push("MC/DC「組合せ」分组");
    var identity = constraints.test_rules_ref || (constraints.test_policy_sha256 ? String(constraints.test_policy_sha256).slice(0, 12) : "");
    return items.join(" · ") + (identity ? " · " + identity : "");
  }
  function coverage(execution) {
    var c = execution && execution.coverage;
    if (!c) return "—";
    if (execution.evidence_source === "HOST_EXECUTION") {
      return '<div class="ut-cov-mini">' + [["lines", "行"], ["branches", "分支"], ["mcdc", "MC/DC"]].map(function (k) {
        var metric = c[k[0]] || {};
        var measured = typeof metric.percent === "number" && metric.percent >= 0;
        return '<span><b>' + k[1] + "</b> " + (measured ? esc(metric.percent) + "%" +
          (typeof metric.covered === "number" && typeof metric.total === "number"
            ? "（" + esc(metric.covered) + " / " + esc(metric.total) + "）" : "")
          : esc(metric.status || "未测量")) + "</span>";
      }).join("") + "</div>";
    }
    return '<div class="ut-cov-mini">' + [["c0", "C0"], ["c1", "C1"], ["mcdc", "MC/DC"]].map(function (k) {
      var v = c[k[0]], measured = typeof v === "number" && v >= 0;
      return '<span class="' + (!measured ? "is-missing" : v >= 100 ? "is-full" : "is-gap") +
        '"><b>' + k[1] + "</b> " + (measured ? esc(v + "%") : "—") + "</span>";
    }).join("") + "</div>";
  }
  // The linked build ut-agent chose for this run (map evidence), by folder name.
  function stubBuild(execution) {
    var s = execution && execution.stub_build;
    if (!s || !s.selected) return "";
    return '<div class="submuted" title="' + esc(s.selected) + '">构建 ' +
      esc(String(s.selected).split(/[\\/]/).pop()) + "</div>";
  }

  function hostEvidence(attempt) {
    var execution = attempt.execution || {};
    var panel = $("ut-d-host");
    panel.hidden = execution.evidence_source !== "HOST_EXECUTION";
    if (panel.hidden) return;
    $("ut-d-host-summary").innerHTML = "<p><b>" + esc(execution.status || "") + "</b> · " +
      esc(execution.reason || "") + "</p>" + coverage(execution);
    var generation = execution.stub_generation || {};
    $("ut-d-stub-rounds").innerHTML = "<p>Stub 模式：" + esc(generation.mode || "deterministic") +
      (generation.model ? " · 模型：" + esc(generation.model) : "") +
      (generation.skipped ? " · " + esc(generation.skipped) : "") + "</p>" +
      ((generation.rounds || []).length ? '<table class="ut-issue-table"><thead><tr><th>轮次</th><th>状态</th>' +
        '<th>Token（输入 / 输出）</th><th>说明</th></tr></thead><tbody>' + generation.rounds.map(function (round) {
          var usage = round.usage || {};
          return "<tr><td>" + esc(round.round) + "</td><td>" + esc(round.status) + "</td><td>" +
            esc(usage.input_tokens == null ? "—" : usage.input_tokens) + " / " +
            esc(usage.output_tokens == null ? "—" : usage.output_tokens) + "</td><td>" +
            esc(round.reason || round.proposal_status || "") + "</td></tr>";
        }).join("") + "</tbody></table>" : "");
    $("ut-d-host-artifacts").innerHTML = '<ul class="ut-artifact-list">' + (generation.artifacts || []).map(function (artifact) {
      return '<li><a href="' + esc(LMApi.utArtifactUrl(pid, job.id, attempt.id, artifact.id)) + '">' +
        esc(artifact.name) + '</a> <code class="submuted">' + esc((artifact.sha256 || "").slice(0, 12)) + "</code></li>";
    }).join("") + "</ul>";
  }
  // The qualifier (source version, model name) goes on a muted second line so
  // the column stays as narrow as the origin word itself.
  function originOf(a) {
    var r = a.reviser || {};
    var o = esc(U.ORIGIN[a.origin] || a.origin);
    var detail = a.origin === "CAPTURE" && r.from_attempt ? "来自第 " + r.from_attempt + " 版"
      : a.origin === "LLM" && r.model ? r.model : "";
    return detail ? o + '<div class="submuted">' + wrapCode(detail) + "</div>" : o;
  }
  // A model round only appends input rows; say what it added, what the
  // platform refused and what it declined before its own rationale.
  function roundNote(a) {
    var r = a.reviser || {};
    var parts = [];
    if (a.origin === "LLM" && Array.isArray(r.rows)) {
      if (r.rows.length) {
        parts.push("新增 " + r.rows.length + " 行：" + r.rows.map(function (row) {
          return "#" + row.row + " " + row.obligation;
        }).join("、"));
      }
      if ((r.rejected || []).length) parts.push("拒绝 " + r.rejected.length + " 行");
      if ((r.declined || []).length) parts.push("声明无法补齐 " + r.declined.join("、"));
    }
    var text = r.rationale || r.error || a.note || "";
    if (text && parts.indexOf(text) < 0) parts.push(text);
    return parts.join("；");
  }

  function renderSync() {
    var last = detailSyncedAt ? "最近成功同步：" + U.time(detailSyncedAt) : "尚未成功同步";
    var mode = detailSyncFailed ? "刷新失败，当前为上次状态；请重试刷新。"
      : !job ? "正在加载作业状态。"
      : !detailFresh ? "状态待同步。"
      : U.ACTIVE[job.state] ? (document.hidden ? "页面隐藏，自动刷新已暂停。" : "每 3 秒自动刷新。")
      : "当前无需自动刷新，可手动刷新。";
    $("ut-d-sync").textContent = mode + " " + last;
    $("ut-d-progress").classList.toggle("is-stale", !detailFresh);
  }

  function staleDetail() {
    detailFresh = false;
    detailSyncFailed = true;
    setActionBusy(true);
    renderSync();
  }

  // ut-agent live generation phases (ut-agent.generation-progress/1).
  var GEN_PHASE = {
    queued: "等待生成进程", generating: "开始生成", deriving_obligations: "推导测试义务",
    solving: "求解测试义务", validating: "校验用例集", writing_artifacts: "写出 TestCsv 等产物",
    finished: "本函数已完成，等待批次内其他函数"
  };
  var GEN_STALE_SECONDS = 60;

  function megaSteps(n) {
    return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  }

  function stepsLine(used, limit) {
    return "语义步数 " + megaSteps(used) + " / " + megaSteps(limit) +
      "（预算占比 " + Math.min(100, Math.floor(used * 100 / limit)) + "%）";
  }

  // Generation detail under the "用例生成" step: live snapshot while the job
  // generates, the final step count afterwards. Steps are an upper-bound
  // budget, never a time estimate, so the copy says so.
  function generationDetail() {
    var gen = job.generation || {};
    if (job.state !== "generating") {
      if (gen.semantic_steps == null || !gen.step_limit) return null;
      var exhausted = /GENERATION_BUDGET_EXHAUSTED/.test(gen.error || "");
      return { summary: null, html: esc(stepsLine(gen.semantic_steps, gen.step_limit)) +
        (exhausted ? "<br>超出语义步数预算，生成已安全停止，未产出部分结果" : "") };
    }
    var live = job.generation_progress;
    if (!live) return null;
    var fn = live.function, project = live.project;
    if (!fn || fn.phase === "queued") {
      var waiting = project && project.phase === "extracting"
        ? "正在抽取源码结构（整批 " + project.target_count + " 个函数共用一次 Clang 抽取）"
        : fn ? "源码结构已抽取，等待空闲的生成进程"
        : "尚未开始：等待生成资源，同一时间只运行一个生成批次";
      return { summary: waiting + "。", html: esc(waiting) };
    }
    var label = GEN_PHASE[fn.phase] || fn.phase;
    if (fn.obligations_total) label += " · 测试义务 " + fn.obligations_done + " / " + fn.obligations_total;
    var html = esc(label);
    if (fn.steps_used != null && fn.step_limit) {
      html += '<br><progress class="ut-gen-meter" max="' + fn.step_limit + '" value="' +
        Math.min(fn.steps_used, fn.step_limit) + '"></progress>' + esc(stepsLine(fn.steps_used, fn.step_limit)) +
        "<br>步数是生成上限的消耗比例，不代表剩余时间；多数函数远未用完预算即结束";
    }
    var age = (Date.parse(live.observed_at) - Date.parse(fn.updated_at)) / 1000;
    html += "<br>进度更新于 " + esc(U.time(fn.updated_at));
    if (age > GEN_STALE_SECONDS) {
      html += '<br><span class="ut-gen-stale">进度已 ' + Math.round(age) + " 秒未更新，生成进程可能卡住</span>";
    }
    return { summary: "正在生成：" + label + "。", html: html };
  }

  function renderProgress() {
    var state = job.state, attempts = job.attempts || [];
    var active = !!U.ACTIVE[state];
    var decided = state === "approved" || state === "rejected";
    var generated = job.has_reference || attempts.some(function (a) { return a.origin === "GENERATOR" && a.has_csv; });
    var checked = attempts.some(function (a) { return !!a.verdict; });
    var current = state === "queued" ? 0 : state === "generating" ? 1
      : state === "executing" || state === "repairing" ? 2 : 3;
    var summary = state === "queued" ? "作业已入队，等待后台调度。"
      : state === "generating" ? "本批次处于用例生成阶段，当前函数的生成结果尚未返回；可能包含资源等待。"
      : state === "executing" ? "处于执行与校验阶段，可能包含调度或执行资源等待。"
      : state === "repairing" ? "处于模型修正阶段；候选通过检查后会返回执行与校验。"
      : job.reason || (decided ? U.STATE[state][1] : (U.STATE[state] || [null, state])[1]);
    var generation = generationDetail();
    if (generation && generation.summary) summary = generation.summary;
    if (job.stop_requested && active) summary = "已请求停止，等待后台在步骤边界处理；已得到的结果会保留。";
    if (job.superseded_by) summary = "历史作业，已被 #" + job.superseded_by + " 取代。" + summary;
    $("ut-d-progress").hidden = false;
    $("ut-d-progress-summary").textContent = summary;
    var stages = [
      ["排队调度", state === "queued" ? "等待调度" : "已入队"],
      ["用例生成", state === "generating" ? "当前阶段" : generated ? "已产出基准版本"
        : job.gate_code === "GENERATION_FAILED" ? "生成失败"
        : job.gate_code === "NO_TESTCSV" ? "未产出 CSV" : active && current < 1 ? "等待前序阶段" : "无产出记录"],
      ["执行与校验", current === 2 ? (state === "repairing" ? "模型修正中" : "当前阶段")
        : checked ? "已有校验记录" : active ? "等待前序阶段" : "无校验记录"],
      ["人工审核", active ? "等待流程交接" : (U.STATE[state] || [null, state])[1]]
    ];
    var rounds = "已完成修正 " + job.repair_rounds + " / " + job.max_repair_rounds + " 轮";
    var repairNote = state === "repairing" ? "正在修正 → 候选检查 → 再次执行与校验"
      : job.gate_code === "MODEL_UNAVAILABLE" ? "模型调用异常"
      : job.repair_rounds ? "修正后返回执行与校验，按结果决定是否继续"
      : "未触发模型修正";
    $("ut-d-progress-steps").innerHTML = stages.map(function (stage, index) {
      // Inactive jobs expose evidence only: an error does not prove which step ran.
      var kind = active && !job.superseded_by && !job.stop_requested && index === current ? "current"
        : (index === 0 && active && current > 0) || (index === 1 && generated) ? "done"
        : index === 3 && state === "approved" ? "done"
        : index === 3 && !active ? "attention" : "pending";
      return '<li class="ut-progress-step is-' + kind + '"' +
        (kind === "current" ? ' aria-current="step"' : "") + '><span class="ut-progress-marker" aria-hidden="true">' +
        (kind === "done" ? "&#10003;" : index + 1) + '</span><div class="ut-progress-step-head"><strong>' +
        esc(stage[0]) + "</strong><span>" + esc(stage[1]) + "</span></div>" +
        (index === 1 && generation ? '<p class="ut-progress-loop">' + generation.html + "</p>" : "") +
        (index === 2 ? '<p class="ut-progress-loop">模型修正 ↔ 执行与校验<br>' + esc(rounds) + " · " + esc(repairNote) + "</p>" : "") + "</li>";
    }).join("");
    $("ut-d-review-section").hidden = active;
    renderSync();
  }

  function renderJob() {
    renderProgress();
    var t = job.target || {};
    var gate = U.actionable(job);
    var shown = U.jobState(job);
    $("ut-d-title").textContent = t.function || ("作业 #" + job.id);
    U.applyState($("ut-d-hstate"), shown);
    U.applyState($("ut-d-state"), shown);
    var banner = $("ut-d-superseded");
    banner.hidden = !job.superseded_by;
    banner.innerHTML = job.superseded_by
      ? "该函数已重新入队，最新作业是 <a href=\"?job=" + job.superseded_by + "\" data-open-job=\"" +
        job.superseded_by + "\">#" + job.superseded_by + "</a>。本作业只保留为历史记录，不能再审核或修订。"
      : "";
    $("ut-d-sub").textContent = "作业 #" + job.id + " · " + (t.source_rel || "");
    $("ut-d-verdict").innerHTML = U.verdictPill(job.last_verdict, job.execution_backend);
    $("ut-d-function").textContent = t.function || "";
    $("ut-d-source").textContent = t.source_rel || "";
    var b = job.batch || {};
    var prov = b.provenance || {};
    var requirements = coverageRequirements(b.constraints || {});
    $("ut-d-batch").textContent = "#" + job.batch_id +
      (prov.baseline_ref ? " · " + prov.baseline_ref : requirements ? " · " + requirements : "") +
      (prov.generator_commit ? " · " + String(prov.generator_commit).slice(0, 8) : "");
    $("ut-d-requirements").textContent = requirements || "—";
    $("ut-d-gen").textContent = job.generation_status || "—";
    $("ut-d-rounds").textContent = job.repair_rounds + " / " + job.max_repair_rounds;
    $("ut-d-review").textContent = job.reviewed_at
      ? "审核人 #" + job.reviewed_by + " · " + U.time(job.reviewed_at) +
        (job.review_note ? " · " + job.review_note : "") : "—";
    $("ut-d-updated").textContent = U.time(job.updated_at);
    $("ut-d-reason").textContent = job.reason || "—";
    var info = U.gateInfo(job);
    $("ut-d-gate-code").hidden = !info;
    $("ut-d-gate-code").textContent = info ? info[0] : "";
    var resumable = job.state === "blocked" || job.state === "error" || job.state === "stopped";
    $("ut-d-retry").hidden = !U.can("edit") || !resumable || !!job.superseded_by;
    $("ut-d-retry-label").textContent = job.state === "stopped" ? "继续" : "重试";
    $("ut-d-stop").hidden = !U.can("edit") || !U.ACTIVE[job.state] || !!job.stop_requested;

    var decided = job.state === "approved" || job.state === "rejected";
    $("ut-d-gate-hint").textContent = gate
      ? (info ? info[1] + " " : job.state === "stopped" ? U.GATE.STOPPED[1] + " " : "") +
        "审核的是当前版本。机器判定未通过时，通过必须填写意见。"
      : job.superseded_by ? "已被更新的作业取代，只读。"
      : job.stop_requested ? "已请求停止：当前调用结束后停止，已得到的结果会保留。"
      : decided
        ? (U.STATE[job.state][1] + (job.reviewed_at ? " · " + U.time(job.reviewed_at) : "") +
           (job.review_note ? " · " + job.review_note : ""))
        : "作业仍在流水线中，结束后才能审核。";
    $("ut-d-gate-forms").hidden = !gate || (!U.can("review") && !U.can("edit"));
    $("ut-d-review-forms").hidden = !U.can("review");
    $("ut-d-revision-forms").hidden = !U.can("edit");
    if (gate && !U.can("review") && !U.can("edit")) {
      $("ut-d-gate-hint").textContent = "等待有权限的成员审核或修订。你可以查看版本和校验证据。";
    }
    setActionBusy(actionPending);

    var dl = $("ut-d-download");
    var current = (job.attempts || []).find(function (a) { return a.id === job.current_attempt_id; });
    dl.hidden = !(current && current.has_csv);
    if (!dl.hidden) dl.href = LMApi.utAttemptUrl(pid, job.id, job.current_attempt_id);

    var revisable = canRevise();
    var runs = fruitlessRuns(job.attempts || []);
    $("ut-d-attempts").innerHTML = (job.attempts || []).map(function (a) {
      var run = runs[a.id];
      var head = run && run.first === a.id ? runRow(run) : "";
      var folded = run && !expandedRuns[run.key] && a.id !== shownAttempt;
      var note = roundNote(a);
      var isCurrent = a.id === job.current_attempt_id;
      var acts = a.has_csv
        ? iconButton("查看表格", ICO.view, 'data-attempt="' + a.id + '" data-act="view"') +
          (isCurrent && revisable
            ? iconButton("在表格中修订", ICO_EDIT, 'data-attempt="' + a.id + '" data-act="edit"') : "") +
          iconLink("下载 CSV", ICO.download, LMApi.utAttemptUrl(pid, job.id, a.id))
        : "";
      return head + '<tr data-attempt="' + a.id + '"' + (isCurrent ? ' class="is-current" title="当前版本"' : "") +
        (run ? ' data-run-member="' + run.key + '"' + (folded ? " hidden" : "") : "") + ">" +
        '<td class="ut-num">' + a.seq + "</td><td>" + originOf(a) + "</td>" +
        "<td>" + (a.verdict ? U.verdictPill(a.verdict,
          a.execution && a.execution.evidence_source === "HOST_EXECUTION" ? "clang" : "winams") :
          (U.ACTIVE[job.state] ? U.statePill("executing") : "—")) + "</td>" +
        '<td class="ut-att-issues">' + topIssues(a.check) + "</td><td>" + coverage(a.execution) +
          stubBuild(a.execution) + "</td>" +
        '<td class="ut-att-note" title="' + esc(note) + '">' + esc(note.slice(0, 60)) + "</td>" +
        '<td class="lm-cell-time"><code>' + esc(U.time(a.created_at)) + "</code></td>" +
        '<td class="lm-row-actions"><span class="row-acts">' + acts + "</span></td></tr>";
    }).join("") || '<tr><td colspan="8" class="muted">尚无版本</td></tr>';
    var wanted = shownAttempt || job.current_attempt_id;
    if (wanted) select(wanted, false);
    else {
      $("ut-d-host").hidden = true;
      $("ut-d-issues-title").textContent = "校验问题";
      $("ut-d-issues-sum").textContent = "";
      $("ut-d-issues").innerHTML = '<p class="muted">尚无版本，尚未校验</p>';
    }
  }

  // What a model round that left no candidate did; mirrors the server's
  // repair-stats classification.
  function fruitless(a) {
    var r = a.reviser || {};
    if (a.origin !== "LLM" || a.has_csv) return null;
    if (r.model_contacted === false) return "guarded";
    if (!("rationale" in r)) return "unusable";
    return (r.rejected || []).length ? "rejected" : "stalled";
  }
  var FRUITLESS_ZH = { stalled: "无进展", rejected: "行被拒", unusable: "回复不可用", guarded: "未调用模型" };

  // Two or more consecutive fruitless rounds fold into one summary row: on
  // real data they were most of the history and pushed the useful versions
  // out of view.
  function fruitlessRuns(attempts) {
    var runs = {}, current = [];
    function close() {
      if (current.length >= 2) {
        var run = { key: job.id + ":" + current[0].seq, first: current[0].id, items: current };
        current.forEach(function (a) { runs[a.id] = run; });
      }
      current = [];
    }
    attempts.forEach(function (a) { if (fruitless(a)) current.push(a); else close(); });
    close();
    return runs;
  }

  function runRow(run) {
    var counts = {};
    run.items.forEach(function (a) { var k = fruitless(a); counts[k] = (counts[k] || 0) + 1; });
    var parts = Object.keys(counts).map(function (k) { return FRUITLESS_ZH[k] + " " + counts[k]; });
    var open = !!expandedRuns[run.key];
    return '<tr class="ut-att-run"><td class="ut-num">' + run.items[0].seq + "–" +
      run.items[run.items.length - 1].seq + '</td><td colspan="6">本地模型 ' + run.items.length +
      " 轮未产出候选 <span class=\"submuted\">· " + esc(parts.join(" · ")) + "</span> " +
      '<button class="btn small ghost" type="button" data-run-toggle="' + run.key + '">' +
      (open ? "收起" : "展开") + "</button></td><td></td></tr>";
  }

  // Choose the attempt whose check result is shown below the history. A
  // historical attempt is labelled as such and blocks review until the
  // current one is chosen again.
  function select(attemptId, pin) {
    var a = (job.attempts || []).find(function (x) { return x.id === attemptId; });
    if (!a) return;
    ++diagnosticEpoch;
    ++previewEpoch;
    if (pin) shownAttempt = attemptId;
    $("ut-d-issues-title").textContent = "校验问题 · " + versionLabel(a) +
      (a.id === job.current_attempt_id ? " · 当前版本" : " · 历史版本，审核前请选择当前版本");
    var rows = $("ut-d-attempts").querySelectorAll("tr[data-attempt]");
    Array.prototype.forEach.call(rows, function (row) {
      row.classList.toggle("is-shown", Number(row.dataset.attempt) === attemptId);
    });
    setActionBusy(actionPending);
    renderIssues(a);
    hostEvidence(a);
  }

  function versionLabel(a) {
    return "第 " + a.seq + " 版（" + (U.ORIGIN[a.origin] || a.origin) + "）";
  }
  function canRevise() {
    return !!(job && U.actionable(job) && U.can("edit") &&
      (job.attempts || []).some(function (a) { return a.id === job.current_attempt_id && a.has_csv; }));
  }

  // ------------------------------------------------------- check issues
  var SEVERITY = { error: ["failed", "错误"], warning: ["pending", "警告"], info: ["notask", "提示"] };
  var DETAIL_KEYS = { row: "用例", column: "列", expected: "期望值", actual: "实际值",
                      before: "原期望值", after: "新期望值",
                      code: "代码", detail: "说明", path: "路径", message: "说明",
                      obligation: "义务", kind: "类型", decision: "判定式",
                      solve: "求解", reason: "原因", gap: "缺口",
                      min: "下限", max: "上限", source: "证据来源", pending: "待捕获输出",
                      verification: "输入复核" };
  Object.assign(DETAIL_KEYS, { branch_id: "判定 ID", line: "源码行", outcome: "目标结果",
    claimed_by: "关联用例", state: "测量状态", metric: "指标", points: "覆盖点统计",
    evidence_source: "证据来源", case_id: "用例 ID", status: "状态",
    reference: "参考桩观测", candidate: "候选桩观测", reference_case_id: "参考用例 ID" });
  Object.assign(DETAIL_KEYS, { annotation: "需恢复的注释／标签", annotations: "参考注释／标签",
    inputs: "对应输入", reference_row: "参考用例（从 0 起）", missing_count: "缺失数量", missing_kind: "缺失内容" });
  // Stored check JSON comes back with sorted keys; show evidence in reading order.
  var KEY_ORDER = ["row", "column", "before", "after", "expected", "actual", "code", "path",
                   "message", "detail", "gap", "obligation", "kind", "decision", "solve", "reason"];
  var PREVIEW_ROWS = 8;

  // Gap classes come from the generator's structured solve evidence. Only a
  // missing case can be closed by adding input rows; the others need evidence
  // or a person, and 100% coverage proves neither.
  var GAP_CLASS = {
    MISSING_CASE: ["缺用例", "is-case"],
    SEMANTIC_EVIDENCE: ["缺语义/环境证据", "is-evidence"],
    INFEASIBILITY_UNCONFIRMED: ["不可达待确认", "is-infeasible"],
    OUTPUT_ORACLE_PENDING: ["仅缺期望值", "is-evidence"],
  };
  var CAUSE_TEXT = {
    VOLATILE_READ_UNPROVEN: function (g) {
      return "单次调用内的读取关系尚未证明：" + (g.unstable_reads || []).join("、") +
        "；需核对类型化读取证据与执行契约";
    },
    SEARCH_INCOMPLETE: function () { return "求解搜索未穷尽，可能存在可达输入"; },
  };

  // An unresolved obligation carries every generator field; keep what a
  // reviewer needs to decide whether an input step is missing.
  function gapRow(g) {
    var kinds = { boundary: "边界值", "input-boundary": "输入最小／最大值", branch: "分支结果", condition: "条件",
      mcdc: "MC/DC 独立影响", case: "分支标签", loop: "循环边界状态", arithmetic: "演算式专项", execution: "函数入口" };
    var boundaries = { above: "边界上方", below: "边界下方", exact: "边界本身", min: "最小值", max: "最大值",
      "loop-entry": "循环进入", "variable-pair": "双操作数取值", "operand-pair": "双操作数取值", "required-point": "必需测试点" };
    var facts = [kinds[g.kind] || g.kind || "测试要求"];
    if (g.outcome != null) facts.push(typeof g.outcome === "boolean" ? (g.outcome ? "真" : "假") : String(g.outcome));
    if (g.condition_index != null) facts.push("条件 " + g.condition_index);
    if (g.boundary_class) {
      facts.push((boundaries[g.boundary_class] || g.boundary_class) +
        (g.boundary_value != null ? " " + g.boundary_value : "") +
        (g.boundary_right_value != null ? ".." + g.boundary_right_value : ""));
    }
    if (g.case_label) facts.push("case " + g.case_label);
    var explain = CAUSE_TEXT[g.solve_cause];
    return { gap: g.gap_class || "", obligation: g.obligation_id, kind: facts.join(" · "),
             decision: String(g.decision || g.description || "").replace(/\s+/g, " "),
             solve: g.solve_status, reason: (explain ? explain(g) + "；" : "") + (g.reason || "未提供原始原因") };
  }
  var ACTION_SCOPE = {
    INPUT_CANDIDATE: "输入用例", SEMANTIC_EVIDENCE: "语义／环境证据", HUMAN_REVIEW: "人工确认",
    MEASUREMENT: "覆盖测量", BUILD_ENVIRONMENT: "构建与执行环境", CANDIDATE_FILE: "候选文件",
    EVIDENCE_INVESTIGATION: "证据排查", EXECUTION_CAPTURE: "执行捕获", ARTIFACT_BINDING: "版本绑定",
    STUB_CANDIDATE: "桩候选", PER_OBLIGATION: "逐项处理", ANNOTATION: "注释／标签",
  };
  function requirementsHtml(context) {
    if (!context) return "";
    var identity = context.rules_ref || "未提供规则版本";
    if (context.status !== "BOUND") {
      return '<div class="ut-guidance"><p><b>本次生成绑定的测试要求：</b>' + esc(identity) +
        '</p><p>' + esc(context.reason || "测试配置快照不足，需核对生成版本。") + '</p></div>';
    }
    return '<div class="ut-guidance"><p><b>本次生成绑定的测试要求：</b>' + esc(identity) + '</p><ul>' +
      (context.requirements || []).map(function (r) {
        var state = r.role === "PRESENTATION" ? (r.required ? "启用，仅影响注释呈现" : "关闭，不要求此类注释") :
          (r.required ? "要求" : "未要求");
        return '<li>' + esc(r.label) + '：<b>' + esc(state) + '</b></li>';
      }).join("") + '</ul></div>';
  }
  function requirementHtml(r) {
    if (!r) return "";
    var config = (r.requirements || []).map(function (item) {
      return item.label + "=" + (item.required ? "要求" : "未要求");
    }).join("；");
    var conflict = r.configuration_status === "NEEDS_REVIEW" ?
      '<p class="ut-issue-text">配置与义务记录需复核，请先核对生成版本及绑定，不能直接要求新增用例。</p>' : '';
    return '<div class="ut-guidance"><p><b>配置依据：</b>' + esc(config || r.rules_ref || "未提供可核对的配置快照") +
      '</p><p><b>具体缺口：</b>' + esc(r.target || "需查看原始义务记录") + '</p><p><b>缺失内容：</b>' +
      (r.missing_kind === "TEST_CASE" ? "输入用例尚未完成证明" : "证据或审核结论，不能靠补注释消除") + '</p>' + conflict + '</div>';
  }
  function guidanceHtml(g) {
    if (!g) return '<p class="ut-issue-text">本报告未提供处理指引，请结合完整诊断和版本文件排查。</p>';
    return '<div class="ut-guidance"><p><b>处理入口：</b>' +
      esc(ACTION_SCOPE[g.action_scope] || g.action_scope || "证据排查") + '</p><ol>' +
      (g.steps || []).map(function (step) { return '<li>' + esc(step) + '</li>'; }).join("") +
      '</ol><p><b>如何确认完成：</b>' + esc(g.verification || "重新校验并核对证据。") + '</p></div>';
  }
  function typedColumns(a) {
    var check = a.check || {}, w = check.candidate_witnesses || {};
    var domain = (check.input_ranges || {}).contract || {}, columns = domain.columns || [];
    if (w.contract !== "ut-agent.candidate-witness/1" ||
        (w.evidence || {}).identified !== true || w.input_mapping_identified !== true ||
        !a.csv_sha256 || check.candidate_sha256 !== a.csv_sha256 ||
        w.candidate_sha256 !== a.csv_sha256 ||
        domain.contract !== "ut-agent.input-domains/1" ||
        !columns.every(function (c, i) { return c.index === i + 1 && typeof c.column === "string"; })) return null;
    return columns;
  }
  function gapPath(a, g) {
    var w = (a.check || {}).candidate_witnesses || {};
    if (!typedColumns(a)) return null;
    return (w.repair_context || []).find(function (r) { return r.obligation_id === g.obligation_id; }) || null;
  }
  function typedFact(value) { return value == null || value === "" ? "UNKNOWN" : plain(value); }
  function typedAtomSummary(atom) {
    var facts = ["变量 " + typedFact(atom.variable), "关系 " + typedFact(atom.operator),
      "边界 " + typedFact(atom.boundary)];
    if (atom.right_operand != null) facts.push("右操作数 " + typedFact(atom.right_operand));
    if (atom.required_outcome != null) facts.push("目标结果 " + plain(atom.required_outcome));
    if (atom.binding) facts.push("绑定 " + plain(atom.binding));
    return facts.join(" · ");
  }
  function pathTokens(path, columns) {
    var found = [], exact = true;
    (path.path || []).forEach(function (node) {
      (node.atoms || []).forEach(function (atom) {
        ["input_tokens", "right_input_tokens", "mask_input_tokens"].forEach(function (key) {
          (atom[key] || []).forEach(function (token) {
            if (!/^I[1-9][0-9]*$/.test(token) || !columns[Number(token.slice(1)) - 1]) exact = false;
            else if (found.indexOf(token) < 0) found.push(token);
          });
        });
      });
    });
    return exact ? found : [];
  }
  function typedPathHtml(a, g, index) {
    var columns = typedColumns(a), path = gapPath(a, g);
    if (!path) return '<p class="ut-issue-text">类型化位置／输入映射未确认（UNKNOWN）；不能从源码、标签或名称推断绑定。</p>';
    var tokens = pathTokens(path, columns);
    return '<div class="ut-guidance"><b>类型化路径与祖先约束</b><ol>' + (path.path || []).map(function (node) {
      var loc = node.location || {};
      var location = typeof loc.file === "string" && Number.isInteger(loc.line) && loc.line > 0
        ? loc.file + ":" + loc.line + (Number.isInteger(loc.column) && loc.column > 0 ? ":" + loc.column : "")
        : "UNKNOWN（未提供类型化源码位置）";
      return '<li><p><b>源码位置：</b><code>' + esc(location) + '</code></p><p>' +
        esc(node.branch_id || "") + ' · 判定：' + esc(node.decision || "UNKNOWN") + ' · 路径要求：' +
        esc(plain(node.required_outcome)) + '</p>' + (node.atoms || []).map(function (atom) {
          var mapping = [];
          ["input_tokens", "right_input_tokens", "mask_input_tokens"].forEach(function (key) {
            (atom[key] || []).forEach(function (token) {
              var c = /^I[1-9][0-9]*$/.test(token) && columns[Number(token.slice(1)) - 1];
              mapping.push(token + " → " + (c ? c.column : "UNKNOWN（无精确列映射）"));
            });
          });
          return '<p><b>类型化约束：</b>' + esc(typedAtomSummary(atom)) + '</p><p><b>精确输入列：</b>' +
            esc(mapping.join("；") || "NO_DIRECT_INPUT／未提供映射，不能按名称替代") +
            '</p><details><summary>原始类型化原子</summary><pre>' + esc(JSON.stringify(atom, null, 2)) + '</pre></details>';
        }).join("") + '</li>';
    }).join("") + '</ol>' + (tokens.length && a.has_csv ?
      '<button class="btn small ghost" type="button" data-gap-inputs="' + index + '" data-gap-attempt="' + a.id +
      '">查看相关 CSV 输入列</button><p>显示现有用例的输入列；缺用例没有可定位的数据行。</p>' : '') + '</div>';
  }
  function gapDetailsHtml(list, a, offset) {
    if (!Array.isArray(list)) return detailHtml(list);
    return list.map(function (g, index) {
      var row = gapRow(g), label = GAP_CLASS[g.gap_class];
      var constraints = (g.constraints || []).map(function (c) {
        return [c.variable, c.operator, c.value].map(plain).join(" ");
      }).join("；");
      var solve = { SAT: "已找到约束解", UNSAT: "当前约束无解，待确认", UNKNOWN: "证据不足或搜索未完成", NOT_SOLVED: "尚未求解" };
      return '<section class="ut-gap-card"><p><b>' +
        esc((g.guidance && g.guidance.title) || (label && label[0]) || "待确认的测试要求") +
        '</b> <code>' + esc(g.obligation_id || "") + '</code></p><dl class="ut-issue-kv">' +
        '<dt>测试目标</dt><dd>' + esc(row.kind) + '</dd><dt>判定／要求</dt><dd>' + esc(row.decision) +
        '</dd><dt>程序检查结果</dt><dd>' + esc((solve[g.solve_status] || "未识别的求解状态") +
          "（" + (g.solve_status || "UNKNOWN") + "） · " + row.reason) + '</dd>' +
        (g.branch_id != null ? '<dt>关联判定</dt><dd>' + esc(String(g.branch_id)) + '</dd>' : '') +
        (g.rule_id ? '<dt>规则依据</dt><dd>' + esc(g.rule_id) + '</dd>' : '') +
        (constraints ? '<dt>已知约束</dt><dd>' + esc(constraints) + '</dd>' : '') +
        '</dl>' + typedPathHtml(a, g, offset + index) + requirementHtml(g.requirement) + guidanceHtml(g.guidance) + '</section>';
    }).join("");
  }
  function rawDiagnosticHtml(issue, paged) {
    return '<details class="ut-diagnostic-raw"><summary>' + (paged ? "本页原始诊断（含证据）" : "完整原始诊断（含证据）") + '</summary><pre>' +
      esc(JSON.stringify(issue, null, 2)) + '</pre></details>';
  }
  async function downloadDiagnostic(attemptId) {
    var a = job && (job.attempts || []).find(function (item) { return item.id === attemptId; });
    if (!a || !a.check || !a.has_csv || !detailFresh || attemptId !== (shownAttempt || job.current_attempt_id)) return;
    var id = job.id, epoch = ++diagnosticEpoch, detail = detailEpoch;
    var sha = a.csv_sha256, seq = a.seq, checkSnapshot = JSON.stringify(a.check), packet;
    try {
      packet = await LMApi.utAttemptDiagnostic(pid, id, attemptId);
      if (epoch !== diagnosticEpoch || detail !== detailEpoch || openJobId !== id || !detailFresh) return;
      if (packet.contract !== "ut-platform.diagnostic-packet/2" || packet.job_id !== id ||
          packet.attempt_id !== attemptId || !sha || packet.candidate_sha256 !== sha ||
          !packet.version || packet.version.seq !== seq || JSON.stringify(packet.check) !== checkSnapshot) {
        throw new Error("诊断包与显示版本不一致，请刷新后重新下载");
      }
    } catch (e) {
      if (epoch === diagnosticEpoch && detail === detailEpoch && openJobId === id) say(e.message, true);
      return;
    }
    var url = window.URL.createObjectURL(new window.Blob([JSON.stringify(packet, null, 2)], { type: "application/json" }));
    var link = document.createElement("a");
    link.href = url;
    link.download = "ut-diagnostic-" + id + "-" + attemptId + ".json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { window.URL.revokeObjectURL(url); }, 1000);
  }
  var DETAIL_VIEW = {
    OUTPUT_ORACLE_PENDING: [function (d) {
      return Array.isArray(d) ? d.map(function (g) {
        return { gap: g.gap_class, obligation: g.obligation_id,
                 pending: (g.pending_outputs || []).join("、"), reason: g.reason };
      }) : d;
    }, ""],
    INPUT_WITNESS_CHECK: [witnessRows, ""],
    INPUT_WITNESS_REJECTED: [witnessRows, ""],
    INPUT_WITNESS_UNKNOWN: [witnessRows, ""],
  };
  function witnessRows(rows) {
    var labels = { WITNESSED: "输入已证实", REJECTED: "不满足义务", UNKNOWN: "证据不足" };
    return Array.isArray(rows) ? rows.map(function (row) {
      return { row: row.row, obligation: row.obligation_id,
               verification: labels[row.status] || row.status,
               actual: row.observed, reason: row.reason };
    }) : rows;
  }

  function clip(text, n) { return text.length > n ? text.slice(0, n) + "…" : text; }
  function plain(v) { return v == null ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v); }

  function coverageHtml(d) {
    return '<div class="ut-cov">' + [["c0", "C0"], ["c1", "C1"], ["mcdc", "MC/DC"]].map(function (k) {
      var value = d[k[0]], ratio = d[k[0] + "_ratio"];
      var measured = typeof value === "number" && value >= 0;
      var of = Array.isArray(ratio) && ratio[1] > 0 ? "（" + ratio[0] + " / " + ratio[1] + "）" : "";
      return '<span class="ut-cov-item ' + (!measured ? "is-missing" : value >= 100 ? "is-full" : "is-gap") +
        '"><b>' + k[1] + "</b> " + (measured ? esc(value + "%" + of) : "未测量") + "</span>";
    }).join("") + "</div>";
  }

  function cellHtml(key, value) {
    if (key === "row" && typeof value === "number") return "#" + (value + 1);
    if (key === "missing_kind") return value === "ANNOTATION" ? "注释／标签" : value === "TEST_CASE" ? "输入用例" : esc(plain(value));
    if (key === "outcome" && typeof value === "boolean") return value ? "真" : "假";
    if (key === "state") {
      var states = { COVERED: "已覆盖", NOT_COVERED: "未覆盖", UNKNOWN: "无法判定" };
      return esc(states[value] || plain(value));
    }
    if (key === "gap") {
      var gap = GAP_CLASS[value];
      return gap ? '<span class="ut-gap ' + gap[1] + '">' + esc(gap[0]) + "</span>" : "—";
    }
    if (key === "column" && typeof value === "string" && window.LMTestCsv) {
      return '<code title="' + esc(value) + '">' + esc(window.LMTestCsv.shortName(value)) + "</code>";
    }
    return esc(clip(plain(value), 160));
  }

  function tableHtml(list, extraClass) {
    var keys = [];
    list.forEach(function (item) {
      Object.keys(item).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
    });
    keys.sort(function (a, b) {
      var x = KEY_ORDER.indexOf(a), y = KEY_ORDER.indexOf(b);
      return (x < 0 ? KEY_ORDER.length : x) - (y < 0 ? KEY_ORDER.length : y);
    });
    function body(items) {
      return items.map(function (item) {
        return "<tr>" + keys.map(function (k) {
          return '<td title="' + esc(plain(item[k])) + '">' + cellHtml(k, item[k]) + "</td>";
        }).join("") + "</tr>";
      }).join("");
    }
    function table(items) {
      return '<table class="ut-issue-table' + (extraClass ? " " + extraClass : "") +
        '"><thead><tr>' + keys.map(function (k) {
        return '<th class="k-' + esc(k) + '">' + esc(DETAIL_KEYS[k] || k) + "</th>";
      }).join("") + "</tr></thead><tbody>" + body(items) + "</tbody></table>";
    }
    var head = list.slice(0, PREVIEW_ROWS), rest = list.slice(PREVIEW_ROWS);
    return table(head) + (rest.length
      ? "<details><summary>其余 " + rest.length + " 条</summary>" + table(rest) + "</details>" : "");
  }

  function detailHtml(d, extraClass) {
    if (d == null || d === "" || (Array.isArray(d) && !d.length)) return "";
    if (typeof d !== "object") return '<p class="ut-issue-text">' + esc(clip(String(d), 600)) + "</p>";
    if (!Array.isArray(d)) {
      if (Array.isArray(d.locations)) {
        return '<p class="ut-issue-text">' + esc((d.metric || "") + " · " + (d.reason || "查看以下覆盖位置")) +
          '</p>' + (d.locations.length ? tableHtml(d.locations, "ut-location-table") :
            '<p class="ut-issue-text">当前报告没有可唯一定位的覆盖点，请先补测量或位置证据。</p>');
      }
      if ("build" in d && Array.isArray(d.cases)) {
        var cells = [];
        d.cases.forEach(function (c) {
          (c.cells || []).forEach(function (cell) {
            if (cell.status === "MATCH") return;
            cells.push({ case_id: c.case_id, column: cell.key, expected: cell.expected,
              actual: cell.observed, status: cell.status, reason: cell.detail });
          });
        });
        return ((d.build && d.build.diagnostics) ? '<pre class="ut-issue-text">' + esc(d.build.diagnostics) + '</pre>' : '') +
          (Array.isArray(d.observation_differences) && d.observation_differences.length ?
            '<p class="ut-issue-text">候选桩与确定性参考适配器的观测差异（不等同期望值差异）：</p>' +
            tableHtml(d.observation_differences, "ut-location-table") : '') +
          (cells.length ? tableHtml(cells, "ut-location-table") : '');
      }
      if ("c0" in d && "c1" in d) return coverageHtml(d);
      return '<dl class="ut-issue-kv">' + Object.keys(d).map(function (k) {
        return "<dt>" + esc(DETAIL_KEYS[k] || k) + "</dt><dd>" + cellHtml(k, d[k]) + "</dd>";
      }).join("") + "</dl>";
    }
    if (d.every(function (x) { return x && typeof x === "object" && !Array.isArray(x); })) {
      return tableHtml(d, extraClass);
    }
    return '<ul class="ut-issue-items">' + d.map(function (x) {
      return "<li>" + esc(clip(plain(x), 300)) + "</li>";
    }).join("") + "</ul>";
  }

  function renderIssues(a) {
    var issues = (a.check && a.check.issues) || [];
    var requirements = requirementsHtml(a.check && a.check.test_requirements);
    var counts = {};
    issues.forEach(function (i) { counts[i.severity] = (counts[i.severity] || 0) + 1; });
    var sum = ["error", "warning", "info"].filter(function (k) {
      return counts[k];
    }).map(function (k) { return counts[k] + " 个" + SEVERITY[k][1]; }).join(" · ");
    $("ut-d-issues-sum").textContent = sum ? "· " + sum : "";
    var completeness = (a.check && a.check.completeness) || {};
    var shownGaps = Array.isArray(completeness.unresolved) ? completeness.unresolved.length : 0;
    var omittedGaps = (completeness.unresolved_count || 0) - shownGaps;
    var shownPending = Array.isArray(completeness.output_pending) ? completeness.output_pending.length : 0;
    var omittedPending = (completeness.output_pending_count || 0) - shownPending;
    // Lists in completeness are authoritative; issue.detail is only a preview.
    issues = issues.slice();
    [["INPUT_OBLIGATION_UNRESOLVED", "unresolved"], ["OUTPUT_ORACLE_PENDING", "output_pending"]].forEach(function (pair) {
      if ((completeness[pair[1]] || []).length && !issues.some(function (i) { return i.code === pair[0]; })) {
        issues.push({ code: pair[0], severity: "warning", message: "未解决测试要求" });
      }
    });
    $("ut-d-issues").innerHTML = requirements + (a.check && a.has_csv ?
      '<p class="ut-diagnostic-tools"><button class="btn small ghost" type="button" data-diagnostic="' +
      a.id + '">下载本地 AI 排查包</button><span class="muted">包含本版本全部输入行、精确列、诊断与候选回复契约</span></p>' : '') +
      (omittedGaps > 0 ? '<p class="ut-issue-text">共 ' + completeness.unresolved_count + ' 项未解决测试要求，本报告只提供前 ' +
        shownGaps + ' 项明细；另有 ' + omittedGaps + ' 项未提供。请重新生成／校验取得完整清单，不能视为已解决。</p>' : '') +
      (omittedPending > 0 ? '<p class="ut-issue-text">共 ' + completeness.output_pending_count + ' 项期望值待回填，本报告只提供 ' +
        shownPending + ' 项明细；另有 ' + omittedPending + ' 项未提供。请重新校验取得完整清单，不能交给模型猜测期望值。</p>' : '') +
      (completeness.details_complete === false ? '<p class="ut-issue-text">校验器声明明细不完整；请重新校验并核对完整报告。</p>' : '') +
      (!issues.length ? '<p class="muted">' + (a.check ? "未报告校验问题" : "尚无校验报告") + '</p>' : '') +
      '<ul class="ut-issue-list">' + issues.map(function (i) {
      var sev = SEVERITY[i.severity] || ["notask", i.severity || "—"];
      var locate = i.code === "ORACLE_MISMATCH" && a.has_csv
        ? '<button class="btn small ghost" type="button" data-sheet="' + a.id + '">在表格中定位</button>' : "";
      var key = i.code === "INPUT_OBLIGATION_UNRESOLVED" ? "unresolved" :
        i.code === "OUTPUT_ORACLE_PENDING" ? "output_pending" : null;
      var list = key && Array.isArray(completeness[key]) ? completeness[key] : i.detail;
      var paged = key && Array.isArray(list), pageKey = a.id + ":" + key;
      var page = paged ? Math.min(gapPages[pageKey] || 0, Math.max(0, Math.ceil(list.length / PREVIEW_ROWS) - 1)) : 0;
      var offset = page * PREVIEW_ROWS, preview = paged ? list.slice(offset, offset + PREVIEW_ROWS) : list;
      function pageButton(to, label) {
        return '<button class="btn small ghost" type="button" data-gap-page="' + to + '" data-gap-kind="' + key +
          '" data-gap-version="' + a.id + '">' + label + '</button>';
      }
      var paging = paged ? '<p>显示 ' + (list.length ? offset + 1 : 0) + '–' + Math.min(offset + PREVIEW_ROWS, list.length) +
        ' / ' + list.length + ' 项 ' + (page ? pageButton(page - 1, "上一页") : '') +
        (offset + PREVIEW_ROWS < list.length ? pageButton(page + 1, "下一页") : '') + '</p>' : '';
      return '<li class="ut-issue sev-' + esc(i.severity || "") + '"><div class="ut-issue-head">' +
        LMPill.html(sev[0], sev[1]) + '<code class="ut-issue-code">' + esc(i.code) + "</code>" +
        '<span class="ut-issue-msg">' + esc((i.guidance && i.guidance.title) || i.message || "") + "</span>" + locate + "</div>" +
        (i.guidance && i.message ? '<p class="ut-issue-text">' + esc(i.message) + '</p>' : '') +
        guidanceHtml(i.guidance) +
        paging + (i.code === "INPUT_OBLIGATION_UNRESOLVED" ? gapDetailsHtml(preview, a, offset) : DETAIL_VIEW[i.code]
          ? detailHtml(DETAIL_VIEW[i.code][0](preview), DETAIL_VIEW[i.code][1])
          : detailHtml(preview)) + rawDiagnosticHtml(paged ? Object.assign({}, i, { detail: preview }) : i, paged) + "</li>";
    }).join("") + "</ul>";
  }

  // ------------------------------------------------------------ CSV drawer
  // Show an attempt's CSV as a table in the step-detail drawer; `edit` opens
  // the current candidate for a human revision that is submitted from there.
  async function openSheet(attemptId, edit, relevantTokens) {
    var a = job && (job.attempts || []).find(function (x) { return x.id === attemptId; });
    if (!a || !a.has_csv) return;
    if (edit && (!canRevise() || attemptId !== job.current_attempt_id || actionPending || !detailFresh)) return;
    select(attemptId, true);
    if (!window.LMStepsEditor || !window.LMTestCsv) { say("表格组件未加载", true); return; }
    var epoch = ++previewEpoch, id = job.id, t = job.target || {};
    var text;
    try {
      text = (await LMApi.utAttemptText(pid, id, attemptId)).text;
    } catch (e) {
      if (epoch === previewEpoch && openJobId === id) say(e.message, true);
      return;
    }
    if (epoch !== previewEpoch || openJobId !== id) return;
    var model = window.LMTestCsv.parse(text);
    if (!model) {
      LMUI.alert({ title: "无法以表格显示", body: "该版本缺少 #COMMENT 列头，不是 WinAMS TestCsv。请下载后查看原文。" });
      return;
    }
    var unchanged = window.LMTestCsv.serialize(model, window.LMTestCsv.toDoc(model));
    // Inputs each branch reads (yellow) and disputed expected values (red)
    // sit in different columns, so the two kinds never overlap.
    var branch;
    if (relevantTokens) {
      var columns = typedColumns(a);
      if (!columns || columns.length !== model.ni || columns.some(function (c, i) { return c.column !== model.columns[i]; })) {
        say("CSV 列与类型化映射不一致，不能定位输入", true); return;
      }
      branch = [];
      model.rows.forEach(function (_row, step) {
        relevantTokens.forEach(function (token) {
          branch.push({ step: step, kind: "input", index: Number(token.slice(1)) - 1, color: "#fff0b3" });
        });
      });
    } else branch = window.LMTestCsv.branchMarks(model);
    window.LMStepsEditor.open({ id: a.id, title: t.function || ("作业 #" + id), steps: window.LMTestCsv.toDoc(model) }, {
      heading: "CSV · " + versionLabel(a) + (edit ? " · 人工修订" : "") +
        (relevantTokens ? " · 黄色：类型化路径对应输入列（所有现有行）；缺用例无行位置" :
          branch.length ? " · 黄色：分支判定读取的输入（点击判定行跳转）" : ""),
      layout: window.LMTestCsv.LAYOUT, readOnly: !edit, references: false,
      marks: window.LMTestCsv.marks(model, a.check).concat(branch),
      saveLabel: "提交修订并执行", closeOnSave: true,
      onSave: edit ? function (_json, doc) {
        var csv = window.LMTestCsv.serialize(model, doc);
        if (csv === unchanged) return Promise.reject(new Error("表格没有改动，无需提交"));
        var note = $("ut-d-revision-note").value;
        return act(function () { return LMApi.utRevise(pid, id, csv, note); },
          "人工修订已提交，正在执行", true);
      } : null,
    });
  }

  async function loadJob(id, epoch) {
    var request = ++detailRequest;
    var response;
    try {
      response = await LMApi.utJob(pid, id);
    } catch (error) {
      if (request !== detailRequest || epoch !== detailEpoch || id !== openJobId) return;
      staleDetail();
      throw error;
    }
    if (request !== detailRequest || epoch !== detailEpoch || id !== openJobId) return;
    job = response;
    if (detailSyncFailed) say("");
    detailFresh = true;
    detailSyncFailed = false;
    detailSyncedAt = new Date().toISOString();
    renderJob();
  }

  function showDetail(on) {
    root.hidden = on;
    $("ut-detail").hidden = !on;
    window.scrollTo(0, 0);
  }

  async function openJob(id, opts) {
    if ((opts || {}).push !== false && window.LMUrl) LMUrl.set({ job: id });
    if (window.LMStepsEditor) window.LMStepsEditor.close();
    var epoch = ++detailEpoch;
    ++previewEpoch;
    openJobId = id;
    job = null;
    detailFresh = false;
    detailSyncedAt = null;
    detailSyncFailed = false;
    $("ut-d-progress").hidden = true;
    $("ut-d-review-section").hidden = true;
    setActionBusy(true);
    shownAttempt = null;
    $("ut-d-note").value = "";
    $("ut-d-revision-note").value = "";
    $("ut-d-title").textContent = "作业 #" + id + " · 加载中…";
    $("ut-d-gate-forms").hidden = true;
    $("ut-d-retry").hidden = true;
    $("ut-d-download").hidden = true;
    $("ut-d-sub").textContent = "";
    U.applyState($("ut-d-hstate"), null);
    U.applyState($("ut-d-state"), null);
    ["verdict", "function", "source", "batch", "gen", "rounds", "review", "updated",
     "reason", "issues", "issues-title", "issues-sum", "gate-hint", "attempts"].forEach(function (key) {
      $("ut-d-" + key).textContent = "";
    });
    say("");
    showDetail(true);
    try { await loadJob(id, epoch); } catch (e) {
      if (epoch !== detailEpoch) return;
      $("ut-d-title").textContent = "作业 #" + id;
      say(e.message, true);
    }
    if (busy()) { clearTimeout(timer); timer = setTimeout(poll, POLL_MS); }
  }

  function closeJob(opts) {
    if ((opts || {}).push !== false && window.LMUrl) LMUrl.set({ job: null });
    if (window.LMStepsEditor) window.LMStepsEditor.close();
    job = null;
    detailFresh = false;
    openJobId = null;
    ++detailEpoch;
    ++previewEpoch;
    showDetail(false);
    refresh();
  }

  function setActionBusy(busy) {
    ["ut-d-approve", "ut-d-reject", "ut-d-retry", "ut-d-import", "ut-d-edit-sheet",
     "ut-d-note", "ut-d-revision-note"].forEach(function (id) {
      $(id).disabled = busy || !detailFresh;
    });
    if (!canRevise()) $("ut-d-edit-sheet").disabled = true;
    if (job && shownAttempt && shownAttempt !== job.current_attempt_id) {
      $("ut-d-approve").disabled = true;
      $("ut-d-reject").disabled = true;
    }
    if (job && !(job.attempts || []).some(function (attempt) {
      return attempt.id === job.current_attempt_id && attempt.has_csv;
    })) $("ut-d-approve").disabled = true;
  }

  // `rethrow` hands a failure back to the caller (the drawer keeps the user's
  // edits and shows the error) instead of alerting.
  async function act(operation, done, rethrow) {
    if (actionPending || !job || !detailFresh) {
      if (rethrow) throw new Error("作业状态正在刷新，请稍后再提交");
      return;
    }
    var actionJobId = job.id;
    ++detailRequest;
    actionPending = true;
    setActionBusy(true);
    try {
      await operation();
      if (openJobId === actionJobId) {
        ++detailRequest;
        detailFresh = false;
        $("ut-d-gate-forms").hidden = true;
        setActionBusy(true);
      }
      say(done);
      shownAttempt = null;
      await refresh();
    } catch (e) {
      if (openJobId === actionJobId) {
        ++detailRequest;
        detailFresh = false;
        setActionBusy(true);
      }
      await refresh();
      if (rethrow) throw e;
      await LMUI.alert({ title: "操作失败", body: e.message });
    } finally {
      actionPending = false;
      setActionBusy(false);
    }
  }

  function syncUrl() {
    if (window.LMUrl) LMUrl.replace({ filter: filter || null, q: query || null, batch: batchId || null });
  }

  // ---------------------------------------------------------------- events
  $("ut-filter").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-filter]");
    if (!b) return;
    filter = b.dataset.filter;
    U.selectFilter(this, filter);
    syncUrl();
    renderRows();
  });
  $("ut-q").addEventListener("input", function () { query = this.value.trim(); syncUrl(); renderRows(); });
  $("ut-batch").addEventListener("change", function () { batchId = this.value; syncUrl(); refresh(); });
  $("ut-rows").addEventListener("click", function (e) {
    var r = e.target.closest("[data-retry]");
    if (r && r.dataset.retry) { retryFromList(Number(r.dataset.retry), r); return; }
    var s = e.target.closest("[data-stop]");
    if (s && s.dataset.stop) { stopFromList(Number(s.dataset.stop), s); return; }
    var b = e.target.closest("[data-job]");
    if (!b) return;
    if (e.preventDefault) e.preventDefault();
    openJob(Number(b.dataset.job));
  });
  $("ut-refresh").addEventListener("click", refresh);
  $("ut-d-refresh").addEventListener("click", refresh);

  $("ut-d-close").addEventListener("click", function () { closeJob(); });
  $("ut-d-attempts").addEventListener("click", function (e) {
    var toggle = e.target.closest("[data-run-toggle]");
    if (toggle && toggle.dataset.runToggle) {
      var key = toggle.dataset.runToggle;
      expandedRuns[key] = !expandedRuns[key];
      if (job) renderJob();
      return;
    }
    var b = e.target.closest("[data-attempt]");
    if (!b) return;
    var attempt = Number(b.dataset.attempt);
    if (b.dataset.act) openSheet(attempt, b.dataset.act === "edit");
    else select(attempt, true);
  });
  $("ut-d-issues").addEventListener("click", function (e) {
    var diagnostic = e.target.closest("[data-diagnostic]");
    if (diagnostic) { downloadDiagnostic(Number(diagnostic.dataset.diagnostic)); return; }
    var page = e.target.closest("[data-gap-page]");
    if (page) {
      var version = (job.attempts || []).find(function (a) { return a.id === Number(page.dataset.gapVersion); });
      if (version) { gapPages[version.id + ":" + page.dataset.gapKind] = Math.max(0, Number(page.dataset.gapPage) || 0); renderIssues(version); }
      return;
    }
    var inputs = e.target.closest("[data-gap-inputs]");
    if (inputs) {
      var a = (job.attempts || []).find(function (x) { return x.id === Number(inputs.dataset.gapAttempt); });
      var g = a && (((a.check || {}).completeness || {}).unresolved || [])[Number(inputs.dataset.gapInputs)];
      var path = g && gapPath(a, g), columns = a && typedColumns(a);
      var tokens = path && pathTokens(path, columns);
      if (tokens && tokens.length) openSheet(a.id, false, tokens);
      return;
    }
    var b = e.target.closest("[data-sheet]");
    if (b) openSheet(Number(b.dataset.sheet), false);
  });
  $("ut-show-old").addEventListener("change", function () { showOld = this.checked; renderRows(); });
  $("ut-batch-stop").addEventListener("click", async function () {
    var id = batchId, active = selectedBatchActive();
    if (!id || !active || !U.can("edit")) return;
    var ok = await LMUI.confirm({
      title: "停止批次 #" + id,
      body: "停止该批次中 " + active + " 个进行中的作业。正在执行的 WinAMS 或模型调用会先完成，" +
        "结果保留；之后可在作业详情中继续。",
      confirmText: "停止批次",
    });
    if (!ok) return;
    try {
      var r = await LMApi.utStopBatch(pid, Number(id));
      say("批次 #" + id + "：已停止 " + r.stopped + " 个" +
        (r.stopping ? "，" + r.stopping + " 个将在当前调用结束后停止" : ""));
    } catch (e) {
      say(e.message, true);
    }
    refresh();
  });
  $("ut-d-superseded").addEventListener("click", function (e) {
    var a = e.target.closest("[data-open-job]");
    if (!a) return;
    if (e.preventDefault) e.preventDefault();
    openJob(Number(a.dataset.openJob));
  });
  $("ut-d-stop").addEventListener("click", function () {
    if (!job || !U.can("edit")) return;
    var id = job.id, queued = job.state === "queued";
    act(function () { return LMApi.utStop(pid, id); },
      queued ? "已停止" : "已请求停止：当前调用结束后停止");
  });
  $("ut-d-retry").addEventListener("click", function () {
    if (!job || !U.can("edit")) return;
    var id = job.id;
    act(function () { return LMApi.utRetry(pid, id); }, "已重新开始");
  });
  $("ut-d-edit-sheet").addEventListener("click", function () {
    if (job && canRevise()) openSheet(job.current_attempt_id, true);
  });
  $("ut-d-import").addEventListener("click", function () {
    if (job && canRevise() && !actionPending) $("ut-d-import-file").click();
  });
  $("ut-d-import-file").addEventListener("change", async function () {
    var file = this.files && this.files[0];
    this.value = "";
    if (!file || !job || !canRevise()) return;
    var id = job.id, note = $("ut-d-revision-note").value;
    var ok = await LMUI.confirm({
      title: "导入人工修订",
      body: "将「" + file.name + "」作为新版本提交，并重新执行 WinAMS 与校验。",
      confirmText: "导入并执行",
    });
    if (!ok || !job || job.id !== id) return;
    act(function () { return LMApi.utReviseFile(pid, id, file, note); },
      "已导入 " + file.name + "，正在执行");
  });
  $("ut-d-approve").addEventListener("click", function () {
    if (!job || !U.can("review")) return;
    if (shownAttempt && shownAttempt !== job.current_attempt_id) return;
    if (!(job.attempts || []).some(function (candidate) {
      return candidate.id === job.current_attempt_id && candidate.has_csv;
    })) return;
    var id = job.id, note = $("ut-d-note").value, attempt = job.current_attempt_id;
    act(function () { return LMApi.utReview(pid, id, "approve", note, attempt); }, "审核通过");
  });
  $("ut-d-reject").addEventListener("click", function () {
    if (!job || !U.can("review")) return;
    if (shownAttempt && shownAttempt !== job.current_attempt_id) return;
    var id = job.id, note = $("ut-d-note").value, attempt = job.current_attempt_id;
    act(function () { return LMApi.utReview(pid, id, "reject", note, attempt); }, "审核驳回");
  });

  if (window.LMUrl) {
    LMUrl.onPop(function (q) {
      filter = q.filter || "";
      query = q.q || "";
      batchId = q.batch || "";
      $("ut-q").value = query;
      U.selectFilter($("ut-filter"), filter);
      if (q.job) openJob(Number(q.job), { push: false });
      else if (openJobId) closeJob({ push: false });
      else refresh();
    });
  }

  (window.LMReady || Promise.resolve()).then(function () {
    // Same return path as the Silver task list, for users sent here from the
    // workspace review queue.
    var back = $("lm-back-workspace");
    if (back && window.LMTaskActions && LMTaskActions.cameFromWorkspace()) {
      back.href = LMTaskActions.backUrl();
      back.hidden = false;
    }
    $("ut-q").value = query;
    U.selectFilter($("ut-filter"), filter);
    var deep = window.LMUrl ? Number(LMUrl.get("job")) : 0;
    refresh().then(function () { if (deep) openJob(deep, { push: false }); });
  });
})(window, document);
