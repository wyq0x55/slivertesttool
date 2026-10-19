/*
 * 任务与运行 of a unit-test project: every ut-agent → WinAMS job, and the job
 * detail where its attempts are inspected and its human gate is worked.
 *
 * Mirrors project_tasks.js: the detail is an inline section toggled over the
 * list, and the open job lives in `?job=` so it can be linked and Back
 * returns to the list. Every machine outcome lands at a human gate
 * (awaiting_review, needs_human, blocked, error); nothing is approved here
 * without a person pressing 通过.
 */
(function (window, document) {
  "use strict";

  var root = document.querySelector(".lm-ut-tasks");
  if (!root) return;
  var pid = Number(root.dataset.project);
  var U = window.LMUt, esc = U.esc, say = U.say;
  var POLL_MS = 3000;
  var BATCH_ZH = { generating: "生成中", running: "执行中", done: "完成", failed: "失败" };

  var jobs = [];
  var filter = (window.LMUrl && LMUrl.get("filter")) || "";
  var query = (window.LMUrl && LMUrl.get("q")) || "";
  var batchId = (window.LMUrl && LMUrl.get("batch")) || "";
  var job = null;
  var openJobId = null;
  var shownAttempt = null;
  var timer = null;
  var detailEpoch = 0;
  var previewEpoch = 0;
  var listEpoch = 0;
  var detailRequest = 0;
  var detailFresh = false;
  var actionPending = false;

  function $(id) { return document.getElementById(id); }

  // ------------------------------------------------------------------ list
  function visible(j) {
    if (!(U.FILTERS[filter] || U.FILTERS[""])(j.state)) return false;
    if (!query) return true;
    var t = j.target || {};
    return ((t.function || "") + " " + (t.source_rel || "")).toLowerCase()
      .indexOf(query.toLowerCase()) >= 0;
  }

  function renderRows() {
    var rows = jobs.filter(visible);
    $("ut-rows").innerHTML = rows.length ? rows.map(function (j) {
      var t = j.target || {};
      return "<tr>" +
        '<td class="ut-num">#' + j.id + "</td>" +
        "<td><code>" + esc(t.function || "") + "</code></td>" +
        '<td class="ut-src" title="' + esc(t.source_rel || "") + '">' + esc(t.source_name || "") + "</td>" +
        '<td class="ut-num">#' + j.batch_id + "</td>" +
        "<td>" + U.statePill(j.state) + "</td>" +
        "<td>" + U.verdictPill(j.last_verdict) + "</td>" +
        '<td class="ut-num">' + j.repair_rounds + " / " + j.max_repair_rounds + "</td>" +
        '<td class="lm-cell-time"><code>' + esc(U.time(j.updated_at)) + "</code></td>" +
        '<td class="ut-act"><button class="btn small" type="button" data-job="' + j.id + '">' +
        (U.GATES[j.state] ? "审核" : "查看") + "</button></td></tr>";
    }).join("") : '<tr><td colspan="9" class="muted">' +
      (jobs.length ? "没有符合条件的作业" : "还没有作业：在「实施一览」勾选函数后加入队列") + "</td></tr>";
  }

  async function loadBatches() {
    var batches = await LMApi.utBatches(pid);
    var sel = $("ut-batch");
    sel.innerHTML = '<option value="">全部批次</option>' + batches.map(function (b) {
      var n = Object.keys(b.states).reduce(function (s, k) { return s + b.states[k]; }, 0);
      var prov = b.provenance || {};
      return '<option value="' + b.id + '">#' + b.id + " · " + esc(BATCH_ZH[b.status] || b.status) +
        " · " + n + " 个作业" + (prov.baseline_ref ? " · " + esc(prov.baseline_ref) : "") + "</option>";
    }).join("");
    sel.value = batchId;
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

  async function refresh() {
    clearTimeout(timer);
    try {
      await Promise.all([loadJobs(), loadBatches()]);
      if (openJobId) await loadJob(openJobId, detailEpoch);
    } catch (e) { say(e.message, true); }
    if (busy()) timer = setTimeout(refresh, POLL_MS);
  }

  // ---------------------------------------------------------------- detail
  function topIssues(check) {
    if (!check) return "—";
    return (check.issues || []).filter(function (i) { return i.severity === "error"; })
      .slice(0, 3).map(function (i) { return i.code; }).join(", ") || "—";
  }
  function coverage(execution) {
    var c = execution && execution.coverage;
    if (!c) return "—";
    return ["c0", "c1", "mcdc"].map(function (k) {
      return k.toUpperCase() + " " + (c[k] == null || c[k] < 0 ? "—" : c[k] + "%");
    }).join(" / ");
  }
  function originOf(a) {
    var r = a.reviser || {};
    var o = U.ORIGIN[a.origin] || a.origin;
    return a.origin === "LLM" && r.model ? o + "（" + r.model + "）" : o;
  }

  function renderJob() {
    var t = job.target || {};
    var gate = !!U.GATES[job.state];
    $("ut-d-title").textContent = t.function || ("作业 #" + job.id);
    U.applyState($("ut-d-hstate"), job.state);
    U.applyState($("ut-d-state"), job.state);
    $("ut-d-sub").textContent = "作业 #" + job.id + " · " + (t.source_rel || "");
    $("ut-d-verdict").innerHTML = U.verdictPill(job.last_verdict);
    $("ut-d-function").textContent = t.function || "";
    $("ut-d-source").textContent = t.source_rel || "";
    var b = job.batch || {};
    var prov = b.provenance || {};
    $("ut-d-batch").textContent = "#" + job.batch_id +
      (prov.baseline_ref ? " · " + prov.baseline_ref : "") +
      (prov.generator_commit ? " · " + String(prov.generator_commit).slice(0, 8) : "");
    $("ut-d-gen").textContent = job.generation_status || "—";
    $("ut-d-rounds").textContent = job.repair_rounds + " / " + job.max_repair_rounds;
    $("ut-d-review").textContent = job.reviewed_at
      ? "审核人 #" + job.reviewed_by + " · " + U.time(job.reviewed_at) +
        (job.review_note ? " · " + job.review_note : "") : "—";
    $("ut-d-updated").textContent = U.time(job.updated_at);
    $("ut-d-reason").textContent = job.reason || "—";
    $("ut-d-retry").hidden = !U.can("edit") || !(job.state === "blocked" || job.state === "error");

    var decided = job.state === "approved" || job.state === "rejected";
    $("ut-d-gate-hint").textContent = gate
      ? "审核的是当前版本。机器判定未通过时，通过必须填写意见。"
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

    $("ut-d-attempts").innerHTML = (job.attempts || []).map(function (a) {
      var r = a.reviser || {};
      var note = r.rationale || r.error || a.note || "";
      return "<tr" + (a.id === job.current_attempt_id ? ' class="is-current" title="当前版本"' : "") + ">" +
        '<td class="ut-num">' + a.seq + "</td><td>" + esc(originOf(a)) + "</td>" +
        "<td>" + (a.verdict ? U.verdictPill(a.verdict) :
          (U.ACTIVE[job.state] ? U.statePill("executing") : "—")) + "</td>" +
        "<td>" + esc(topIssues(a.check)) + "</td><td>" + esc(coverage(a.execution)) + "</td>" +
        '<td title="' + esc(note) + '">' + esc(note.slice(0, 60)) + "</td>" +
        '<td class="lm-cell-time"><code>' + esc(U.time(a.created_at)) + "</code></td>" +
        '<td class="ut-act">' + (a.has_csv ? '<button class="btn small ghost" type="button" data-attempt="' +
          a.id + '">预览</button>' : "") + "</td></tr>";
    }).join("") || '<tr><td colspan="8" class="muted">尚无版本</td></tr>';
    var wanted = shownAttempt || job.current_attempt_id;
    if (wanted) preview(wanted, false);
    else {
      ++previewEpoch;
      $("ut-d-csv").textContent = "尚无 CSV";
      $("ut-d-issues").textContent = "尚未校验";
    }
  }

  async function preview(attemptId, pin) {
    var a = (job.attempts || []).find(function (x) { return x.id === attemptId; });
    if (!a) return;
    var epoch = ++previewEpoch;
    var id = job.id;
    if (pin) shownAttempt = attemptId;
    $("ut-d-csv-title").textContent = "CSV · 第 " + a.seq + " 版（" + (U.ORIGIN[a.origin] || a.origin) +
      "）" + (a.id === job.current_attempt_id ? " · 当前版本" : " · 历史版本，审核前请选择当前版本");
    setActionBusy(actionPending);
    var issues = (a.check && a.check.issues) || [];
    $("ut-d-issues").textContent = issues.length ? issues.map(function (i) {
      return "[" + i.severity + "] " + i.code + "  " + i.message +
        (i.detail ? "\n    " + JSON.stringify(i.detail).slice(0, 600) : "");
    }).join("\n") : (a.check ? "未报告校验问题" : "尚无校验报告");
    if (!a.has_csv) { $("ut-d-csv").textContent = "该版本没有 CSV"; return; }
    $("ut-d-csv").textContent = "加载中…";
    try {
      var response = await LMApi.utAttemptText(pid, id, attemptId);
      if (epoch === previewEpoch && openJobId === id) $("ut-d-csv").textContent = response.text;
    } catch (e) {
      if (epoch === previewEpoch && openJobId === id) $("ut-d-csv").textContent = e.message;
    }
  }

  async function loadJob(id, epoch) {
    var request = ++detailRequest;
    var response;
    try {
      response = await LMApi.utJob(pid, id);
    } catch (error) {
      if (request !== detailRequest || epoch !== detailEpoch || id !== openJobId) return;
      detailFresh = false;
      setActionBusy(true);
      throw error;
    }
    if (request !== detailRequest || epoch !== detailEpoch || id !== openJobId) return;
    job = response;
    detailFresh = true;
    renderJob();
  }

  function showDetail(on) {
    root.hidden = on;
    $("ut-detail").hidden = !on;
    window.scrollTo(0, 0);
  }

  async function openJob(id, opts) {
    if ((opts || {}).push !== false && window.LMUrl) LMUrl.set({ job: id });
    var epoch = ++detailEpoch;
    ++previewEpoch;
    openJobId = id;
    job = null;
    detailFresh = false;
    setActionBusy(true);
    shownAttempt = null;
    $("ut-d-note").value = "";
    $("ut-d-revision").value = "";
    $("ut-d-revision-note").value = "";
    $("ut-d-title").textContent = "作业 #" + id + " · 加载中…";
    $("ut-d-gate-forms").hidden = true;
    $("ut-d-retry").hidden = true;
    $("ut-d-download").hidden = true;
    $("ut-d-sub").textContent = "";
    U.applyState($("ut-d-hstate"), null);
    U.applyState($("ut-d-state"), null);
    ["verdict", "function", "source", "batch", "gen", "rounds", "review", "updated",
     "reason", "csv", "issues", "gate-hint", "attempts"].forEach(function (key) {
      $("ut-d-" + key).textContent = "";
    });
    say("");
    showDetail(true);
    try { await loadJob(id, epoch); } catch (e) {
      if (epoch !== detailEpoch) return;
      $("ut-d-title").textContent = "作业 #" + id;
      say(e.message, true);
    }
    if (busy()) { clearTimeout(timer); timer = setTimeout(refresh, POLL_MS); }
  }

  function closeJob(opts) {
    if ((opts || {}).push !== false && window.LMUrl) LMUrl.set({ job: null });
    job = null;
    detailFresh = false;
    openJobId = null;
    ++detailEpoch;
    ++previewEpoch;
    showDetail(false);
    refresh();
  }

  function setActionBusy(busy) {
    ["ut-d-approve", "ut-d-reject", "ut-d-revise", "ut-d-retry", "ut-d-load-current",
     "ut-d-note", "ut-d-revision", "ut-d-revision-note"].forEach(function (id) {
      $(id).disabled = busy || !detailFresh;
    });
    if (job && shownAttempt && shownAttempt !== job.current_attempt_id) {
      $("ut-d-approve").disabled = true;
      $("ut-d-reject").disabled = true;
    }
    if (job && !(job.attempts || []).some(function (attempt) {
      return attempt.id === job.current_attempt_id && attempt.has_csv;
    })) $("ut-d-approve").disabled = true;
  }

  async function act(operation, done) {
    if (actionPending || !job || !detailFresh) return;
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
    var b = e.target.closest("button[data-job]");
    if (b) openJob(Number(b.dataset.job));
  });
  $("ut-refresh").addEventListener("click", refresh);
  $("ut-d-refresh").addEventListener("click", refresh);

  $("ut-d-close").addEventListener("click", function () { closeJob(); });
  $("ut-d-attempts").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-attempt]");
    if (b) preview(Number(b.dataset.attempt), true);
  });
  $("ut-d-retry").addEventListener("click", function () {
    if (!job || !U.can("edit")) return;
    var id = job.id;
    act(function () { return LMApi.utRetry(pid, id); }, "已重新开始");
  });
  $("ut-d-load-current").addEventListener("click", async function () {
    if (!job || !job.current_attempt_id) return;
    var id = job.id;
    var attempt = job.current_attempt_id;
    try {
      var data = await LMApi.utAttemptText(pid, id, attempt);
      if (job && job.id === id && job.current_attempt_id === attempt) $("ut-d-revision").value = data.text;
    } catch (e) { say(e.message, true); }
  });
  $("ut-d-revise").addEventListener("click", function () {
    if (!job || !U.can("edit")) return;
    var text = $("ut-d-revision").value;
    if (!text.trim()) { LMUI.alert({ title: "缺少 CSV", body: "请先粘贴修订后的完整 CSV。" }); return; }
    var id = job.id, note = $("ut-d-revision-note").value;
    act(function () { return LMApi.utRevise(pid, id, text, note); }, "人工修订已提交，正在执行");
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
