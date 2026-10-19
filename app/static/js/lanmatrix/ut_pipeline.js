/*
 * 単体テスト page: 実施一覧, queue, per-job attempt history and human gates.
 *
 * Every machine outcome lands at a human gate (awaiting_review, needs_human,
 * blocked, error); this page is where those gates are worked.  The list polls
 * while any job is still active (queued / generating / executing / repairing).
 */
(function (window, document) {
  "use strict";

  var root = document.querySelector(".lm-ut");
  if (!root) return;
  var pid = Number(root.dataset.project);

  var STATE = {
    queued: ["queued", "排队中"], generating: ["running", "生成中"],
    executing: ["running", "WinAMS 执行中"], repairing: ["running", "模型修正中"],
    awaiting_review: ["pending", "待审核"], needs_human: ["error", "需人工处理"],
    blocked: ["error", "环境阻塞"], error: ["failed", "异常"],
    approved: ["passed", "已通过"], rejected: ["cancelled", "已驳回"]
  };
  var ACTIVE = { queued: 1, generating: 1, executing: 1, repairing: 1 };
  var GATES = { awaiting_review: 1, needs_human: 1, blocked: 1, error: 1 };
  var ORIGIN = { GENERATOR: "生成器", LLM: "本地模型", HUMAN: "人工" };
  var CONFIG_KEYS = ["manifest_path", "index_csv", "product_root", "work_root",
                     "max_repair_rounds", "repair_model"];
  var POLL_MS = 3000;

  var targets = [];
  var filter = "";
  var selected = new Set();
  var job = null;
  var shownAttempt = null;
  var timer = null;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pill(state) {
    var m = STATE[state] || ["notask", "未执行"];
    return '<span class="pill st-' + m[0] + '"><span class="dot"></span>' + esc(m[1]) + "</span>";
  }
  function say(msg, isError) {
    var el = $("ut-status");
    el.textContent = msg || "";
    el.style.color = isError ? "var(--danger)" : "";
  }
  function fail(e) { say((e && e.message) || String(e), true); }
  function busy() {
    return targets.some(function (t) { return t.latest_job && ACTIVE[t.latest_job.state]; }) ||
      (job && ACTIVE[job.state]);
  }
  function schedule() {
    clearTimeout(timer);
    if (busy()) timer = setTimeout(refresh, POLL_MS);
  }

  // ------------------------------------------------------------------ list
  function matches(t) {
    var j = t.latest_job;
    if (!filter) return true;
    if (filter === "none") return !j;
    if (!j) return false;
    if (filter === "active") return !!ACTIVE[j.state];
    return j.state === filter;
  }

  function renderRows() {
    var counts = {};
    targets.forEach(function (t) {
      var key = t.latest_job ? t.latest_job.state : "none";
      counts[key] = (counts[key] || 0) + 1;
    });
    $("ut-counts").textContent = "共 " + targets.length + " 个函数 · 待审核 " +
      (counts.awaiting_review || 0) + " · 需人工 " + (counts.needs_human || 0) +
      " · 已通过 " + (counts.approved || 0);
    var rows = targets.filter(matches);
    if (!rows.length) {
      $("ut-rows").innerHTML = '<tr><td colspan="8" class="muted">' +
        (targets.length ? "没有符合条件的函数" : "尚未读取索引：配置项目绑定后点击「重新读取索引」") +
        "</td></tr>";
    } else {
      $("ut-rows").innerHTML = rows.map(function (t) {
        var j = t.latest_job;
        var locked = !t.in_index || (j && ACTIVE[j.state]);
        return "<tr" + (t.in_index ? "" : ' class="muted"') + ">" +
          '<td><input type="checkbox" data-target="' + t.id + '"' +
          (selected.has(t.id) ? " checked" : "") + (locked ? " disabled" : "") + "></td>" +
          "<td>" + esc(t.row_no) + "</td>" +
          '<td title="' + esc(t.source_rel) + '">' + esc(t.source_name) + "</td>" +
          "<td><code>" + esc(t.function) + "</code>" + (t.in_index ? "" : "（已不在索引）") + "</td>" +
          "<td>" + pill(j && j.state) + "</td>" +
          "<td>" + (j ? esc(j.repair_rounds) : "—") + "</td>" +
          "<td>" + esc(j && j.last_verdict || "—") + "</td>" +
          "<td>" + (j ? '<button class="btn small" type="button" data-job="' + j.id + '">查看</button>' : "") +
          "</td></tr>";
      }).join("");
    }
    $("ut-enqueue").disabled = selected.size === 0;
    $("ut-enqueue").textContent = selected.size ? "加入队列（" + selected.size + "）" : "加入队列";
  }

  async function loadTargets() {
    targets = await LMApi.utTargets(pid);
    var allowed = new Set(targets.filter(function (t) {
      return t.in_index && !(t.latest_job && ACTIVE[t.latest_job.state]);
    }).map(function (t) { return t.id; }));
    selected.forEach(function (id) { if (!allowed.has(id)) selected.delete(id); });
    renderRows();
  }

  async function loadBatches() {
    var batches = (await LMApi.utBatches(pid)).slice(0, 5);
    $("ut-batches").hidden = !batches.length;
    $("ut-batches-body").innerHTML = batches.map(function (b) {
      var states = Object.keys(b.states).map(function (s) {
        return esc((STATE[s] || [0, s])[1]) + " " + b.states[s];
      }).join(" · ");
      var prov = b.provenance || {};
      return '<div class="row wrap" style="gap:12px;padding:4px 0">' +
        "<b>#" + b.id + "</b><span>" + esc(b.status) + "</span><span>" + states + "</span>" +
        '<span class="muted">' + esc(prov.baseline_ref || "") + " " +
        esc((prov.generator_commit || "").slice(0, 8)) + "</span>" +
        (b.error ? '<span style="color:var(--danger)">' + esc(b.error) + "</span>" : "") + "</div>";
    }).join("");
  }

  async function loadConfig() {
    var c = await LMApi.utGetConfig(pid);
    CONFIG_KEYS.forEach(function (k) { $("ut-c-" + k).value = c ? c[k] : ""; });
    $("ut-config-summary").textContent = c ? "· " + (c.manifest_path || "未设置") : "· 未配置";
    if (!c || !c.manifest_path) $("ut-config").open = true;
  }

  async function refresh() {
    try {
      await Promise.all([loadTargets(), loadBatches()]);
      if (job) await loadJob(job.id);
    } catch (e) { fail(e); }
    schedule();
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
      return k.toUpperCase() + " " + (c[k] == null ? "?" : c[k] + "%");
    }).join(" / ");
  }

  function renderJob() {
    var t = job.target || {};
    $("ut-d-title").textContent = t.function || ("作业 #" + job.id);
    $("ut-d-state").outerHTML = pill(job.state).replace("<span class=\"pill", "<span id=\"ut-d-state\" class=\"pill");
    $("ut-d-sub").textContent = (t.source_rel || "") + " · 批次 #" + job.batch_id +
      " · 生成状态 " + (job.generation_status || "—") +
      " · 模型修正 " + job.repair_rounds + "/" + job.max_repair_rounds;
    $("ut-d-reason").textContent = job.reason || "";
    $("ut-d-retry").hidden = !(job.state === "blocked" || job.state === "error");
    var gate = !!GATES[job.state];
    ["ut-d-revise", "ut-d-approve", "ut-d-reject", "ut-d-load-current"].forEach(function (id) {
      $(id).disabled = !gate;
    });
    $("ut-d-attempts").innerHTML = (job.attempts || []).map(function (a) {
      var r = a.reviser || {};
      var origin = ORIGIN[a.origin] || a.origin;
      if (a.origin === "LLM" && r.model) origin += "（" + r.model + "）";
      var note = r.rationale || r.error || a.note || "";
      return "<tr" + (a.id === job.current_attempt_id ? ' style="font-weight:600"' : "") + ">" +
        "<td>" + a.seq + "</td><td>" + esc(origin) + "</td>" +
        "<td>" + esc(a.verdict || (ACTIVE[job.state] ? "执行中" : "—")) + "</td>" +
        "<td>" + esc(topIssues(a.check)) + "</td><td>" + esc(coverage(a.execution)) + "</td>" +
        '<td title="' + esc(note) + '">' + esc(note.slice(0, 60)) + "</td><td>" +
        (a.has_csv ? '<button class="btn small" type="button" data-attempt="' + a.id + '">预览</button> ' +
          '<a class="btn small ghost" href="' + LMApi.utAttemptUrl(pid, job.id, a.id) + '">下载</a>' : "") +
        "</td></tr>";
    }).join("");
    var wanted = shownAttempt || job.current_attempt_id;
    if (wanted) preview(wanted);
  }

  async function preview(attemptId) {
    var a = (job.attempts || []).find(function (x) { return x.id === attemptId; });
    if (!a) return;
    shownAttempt = attemptId;
    $("ut-d-csv-title").textContent = "CSV · 第 " + a.seq + " 版（" + (ORIGIN[a.origin] || a.origin) + "）";
    var issues = (a.check && a.check.issues) || [];
    $("ut-d-issues").textContent = issues.length ? issues.map(function (i) {
      return "[" + i.severity + "] " + i.code + "  " + i.message +
        (i.detail ? "\n    " + JSON.stringify(i.detail).slice(0, 600) : "");
    }).join("\n") : (a.verdict ? "无问题" : "尚未校验");
    try {
      $("ut-d-csv").textContent = (await LMApi.utAttemptText(pid, job.id, attemptId)).text;
    } catch (e) { $("ut-d-csv").textContent = e.message; }
  }

  async function loadJob(id) {
    job = await LMApi.utJob(pid, id);
    renderJob();
  }

  async function openJob(id) {
    shownAttempt = null;
    root.hidden = true;
    $("ut-detail").hidden = false;
    try { await loadJob(id); } catch (e) { fail(e); }
    schedule();
  }

  function closeJob() {
    job = null;
    $("ut-detail").hidden = true;
    root.hidden = false;
    refresh();
  }

  async function act(promise, done) {
    try {
      await promise;
      say(done);
      shownAttempt = null;
      await refresh();
    } catch (e) { fail(e); alert(e.message); }
  }

  // ---------------------------------------------------------------- events
  $("ut-filter").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-filter]");
    if (!b) return;
    filter = b.dataset.filter;
    this.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
    renderRows();
  });
  $("ut-rows").addEventListener("change", function (e) {
    var id = Number(e.target.dataset.target);
    if (!id) return;
    if (e.target.checked) selected.add(id); else selected.delete(id);
    renderRows();
  });
  $("ut-rows").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-job]");
    if (b) openJob(Number(b.dataset.job));
  });
  $("ut-all").addEventListener("change", function () {
    var on = this.checked;
    document.querySelectorAll("#ut-rows input[data-target]:not(:disabled)").forEach(function (x) {
      if (on) selected.add(Number(x.dataset.target)); else selected.delete(Number(x.dataset.target));
    });
    renderRows();
  });
  $("ut-refresh").addEventListener("click", refresh);
  $("ut-import").addEventListener("click", async function () {
    try {
      say("正在读取索引…");
      var r = await LMApi.utImportTargets(pid);
      say("索引共 " + r.total + " 个函数：新增 " + r.added + "，移出 " + r.removed);
      await refresh();
    } catch (e) { fail(e); }
  });
  $("ut-enqueue").addEventListener("click", async function () {
    var ids = Array.from(selected);
    if (!ids.length) return;
    try {
      var b = await LMApi.utCreateBatch(pid, ids, "");
      selected.clear();
      say("批次 #" + b.id + " 已入队（" + ids.length + " 个函数）：生成中");
      await refresh();
    } catch (e) { fail(e); }
  });
  $("ut-config-save").addEventListener("click", async function () {
    var values = {};
    CONFIG_KEYS.forEach(function (k) { values[k] = $("ut-c-" + k).value.trim(); });
    values.max_repair_rounds = Number(values.max_repair_rounds || 0);
    try {
      await LMApi.utPutConfig(pid, values);
      say("绑定已保存");
      await loadConfig();
    } catch (e) { fail(e); }
  });

  $("ut-d-close").addEventListener("click", closeJob);
  $("ut-d-refresh").addEventListener("click", refresh);
  $("ut-d-attempts").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-attempt]");
    if (b) preview(Number(b.dataset.attempt));
  });
  $("ut-d-retry").addEventListener("click", function () {
    act(LMApi.utRetry(pid, job.id), "已重新开始");
  });
  $("ut-d-load-current").addEventListener("click", async function () {
    if (!job.current_attempt_id) return;
    try {
      $("ut-d-revision").value = (await LMApi.utAttemptText(pid, job.id, job.current_attempt_id)).text;
    } catch (e) { fail(e); }
  });
  $("ut-d-revise").addEventListener("click", function () {
    var text = $("ut-d-revision").value;
    if (!text.trim()) { alert("请先粘贴修订后的 CSV"); return; }
    act(LMApi.utRevise(pid, job.id, text, $("ut-d-revision-note").value), "人工修订已提交，正在执行");
  });
  $("ut-d-approve").addEventListener("click", function () {
    act(LMApi.utReview(pid, job.id, "approve", $("ut-d-note").value), "已通过");
  });
  $("ut-d-reject").addEventListener("click", function () {
    act(LMApi.utReview(pid, job.id, "reject", $("ut-d-note").value), "已驳回");
  });

  (window.LMReady || Promise.resolve()).then(function () {
    loadConfig().catch(fail);
    refresh();
  });
})(window, document);
