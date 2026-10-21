/*
 * 实施一览 of a unit-test project: the functions from the project index, their
 * latest job, and the entry point for queueing a batch. Job history, repair
 * attempts and review live on 任务与运行, exactly as Silver runs do.
 * Polls while any listed job is still active.
 */
(function (window, document) {
  "use strict";

  var root = document.querySelector(".lm-ut");
  if (!root) return;
  var pid = Number(root.dataset.project);
  var U = window.LMUt, esc = U.esc, say = U.say;
  var POLL_MS = 3000;

  var targets = [];
  var filter = (window.LMUrl && LMUrl.get("filter")) || "";
  var query = (window.LMUrl && LMUrl.get("q")) || "";
  var selected = new Set();
  var timer = null;
  var requestEpoch = 0;
  var submitting = false;
  var bound = false;

  function $(id) { return document.getElementById(id); }
  function stateOf(t) { return t.latest_job ? t.latest_job.state : null; }
  function selectable(t) { return U.can("edit") && t.in_index && !U.ACTIVE[stateOf(t)]; }

  function visible(t) {
    if (!(U.FILTERS[filter] || U.FILTERS[""])(stateOf(t))) return false;
    if (!query) return true;
    var q = query.toLowerCase();
    return (t.function + " " + t.source_rel).toLowerCase().indexOf(q) >= 0;
  }

  function row(t) {
    var j = t.latest_job;
    var tip = j && j.state === "approved" && j.last_verdict && j.last_verdict !== "PASS"
      ? "人工审核通过（机器判定 " + j.last_verdict + "）" : "";
    return '<tr' + (t.in_index ? "" : ' class="is-stale" title="已不在当前索引中"') + ">" +
      '<td><input type="checkbox" data-target="' + t.id + '"' +
      (selected.has(t.id) ? " checked" : "") + (selectable(t) ? "" : " disabled") + "></td>" +
      '<td class="ut-num">' + esc(t.row_no) + "</td>" +
      "<td><code>" + esc(t.function) + "</code>" +
        '<div class="ut-src submuted" title="' + esc(t.source_rel) + '">' + esc(t.source_name) + "</div></td>" +
      "<td>" + U.statePill(j && j.state, tip) + "</td>" +
      "<td>" + U.verdictPill(j && j.last_verdict) + "</td>" +
      '<td class="ut-num">' + (j ? esc(j.repair_rounds) : "—") + "</td>" +
      '<td class="ut-act">' + (j ? '<a class="btn small ghost" href="/lanmatrix/projects/' + pid +
        "/tasks?job=" + j.id + '">作业 #' + j.id + "</a>" : "") + "</td></tr>";
  }

  function render() {
    var counts = {};
    targets.forEach(function (t) {
      var s = stateOf(t) || "none";
      counts[s] = (counts[s] || 0) + 1;
    });
    $("ut-counts").textContent = "共 " + targets.length + " 个函数 · 待审核 " +
      (counts.awaiting_review || 0) + " · 需人工 " + (counts.needs_human || 0) +
      " · 审核通过 " + (counts.approved || 0);
    var rows = targets.filter(visible);
    $("ut-rows").innerHTML = rows.length ? rows.map(row).join("") :
      '<tr><td colspan="7" class="muted">' + (targets.length ? "没有符合条件的函数" :
        "尚未读取索引：确认项目绑定后点击「重新读取索引」") + "</td></tr>";
    var boxes = rows.filter(selectable);
    $("ut-all").checked = boxes.length > 0 && boxes.every(function (t) { return selected.has(t.id); });
    $("ut-enqueue").disabled = !bound || submitting || selected.size === 0;
    $("ut-enqueue-label").textContent = selected.size ? "加入队列（" + selected.size + "）" : "加入队列";
  }

  // A hidden tab skips its poll and stops rescheduling; becoming visible again
  // refreshes once, which resumes polling while work is still active.
  var pausedWhileHidden = false;
  function poll() {
    if (document.hidden) { pausedWhileHidden = true; return; }
    refresh();
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && pausedWhileHidden) { pausedWhileHidden = false; refresh(); }
  });

  async function refresh() {
    clearTimeout(timer);
    var epoch = ++requestEpoch;
    try {
      var rows = await LMApi.utTargets(pid);
      if (epoch !== requestEpoch) return;
      targets = rows;
      var ok = new Set(targets.filter(selectable).map(function (t) { return t.id; }));
      selected.forEach(function (id) { if (!ok.has(id)) selected.delete(id); });
      render();
    } catch (e) { say(e.message, true); }
    if (targets.some(function (t) { return U.ACTIVE[stateOf(t)]; })) timer = setTimeout(poll, POLL_MS);
  }

  function syncUrl() {
    if (window.LMUrl) LMUrl.replace({ filter: filter || null, q: query || null });
  }

  // ---------------------------------------------------------------- events
  $("ut-filter").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-filter]");
    if (!b) return;
    filter = b.dataset.filter;
    U.selectFilter(this, filter);
    syncUrl();
    render();
  });
  $("ut-q").addEventListener("input", function () {
    query = this.value.trim();
    syncUrl();
    render();
  });
  $("ut-rows").addEventListener("change", function (e) {
    var id = Number(e.target.dataset.target);
    if (!id) return;
    if (e.target.checked) selected.add(id); else selected.delete(id);
    render();
  });
  $("ut-all").addEventListener("change", function () {
    var on = this.checked;
    targets.filter(visible).filter(selectable).forEach(function (t) {
      if (on) selected.add(t.id); else selected.delete(t.id);
    });
    render();
  });
  $("ut-refresh").addEventListener("click", refresh);
  $("ut-import").addEventListener("click", async function () {
    if (submitting || !bound || !U.can("edit")) return;
    submitting = true;
    $("ut-import").disabled = true;
    render();
    try {
      say("正在读取索引…");
      var r = await LMApi.utImportTargets(pid);
      say("索引共 " + r.total + " 个函数：新增 " + r.added + "，移出 " + r.removed);
      await refresh();
    } catch (e) { say(e.message, true); }
    finally {
      submitting = false;
      $("ut-import").disabled = !bound;
      render();
    }
  });
  $("ut-enqueue").addEventListener("click", async function () {
    var ids = Array.from(selected);
    if (!ids.length || submitting || !bound || !U.can("edit")) return;
    submitting = true;
    $("ut-import").disabled = true;
    render();
    try {
      var b = await LMApi.utCreateBatch(pid, ids, "");
      selected.clear();
      say("批次 #" + b.id + " 已入队（" + ids.length + " 个函数），进度见「任务与运行」");
      await refresh();
    } catch (e) { say(e.message, true); }
    finally {
      submitting = false;
      $("ut-import").disabled = !bound;
      render();
    }
  });

  if (window.LMUrl) LMUrl.onPop(function (q) {
    filter = q.filter || "";
    query = q.q || "";
    $("ut-q").value = query;
    U.selectFilter($("ut-filter"), filter);
    render();
  });

  (window.LMReady || Promise.resolve()).then(function () {
    $("ut-q").value = query;
    U.selectFilter($("ut-filter"), filter);
    $("ut-import").disabled = true;
    LMApi.utGetConfig(pid).then(function (c) {
      bound = !!(c && c.manifest_path);
      $("ut-unbound").hidden = bound;
      $("ut-import").disabled = !bound || submitting;
      render();
    }).catch(function (e) { say(e.message, true); });
    refresh();
  });
})(window, document);
