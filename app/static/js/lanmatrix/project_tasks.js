/* Per-project Upload Tasks: scan a chosen folder in the browser, list its test
 * ids, and upload the ticked ones to /api/v1/projects/<id>/tasks/upload-tree.
 * Only project members reach the endpoints (server-enforced); the page hides
 * the upload UI and shows a notice when the membership probe fails. Task rows
 * stream live progress over SSE and support cancel / download / delete, plus
 * client-side filter + sort and batch report-download / batch delete. */
(function () {
  "use strict";
  const root = document.querySelector(".lm-tasks");
  if (!root) return;
  const pid = Number(root.dataset.project);
  const $ = (id) => document.getElementById(id);

  let entries = [], testIds = [], libEntries = [], stdlibEntries = [];
  let capabilities = { delete: false };
  // Last server snapshot of the facts the submit preflight bar reports on.
  let lastModels = [], lastLicense = null, lastRole = "";
  const streams = {};   // task_id -> EventSource
  let allTasks = [];                        // latest snapshot from the server
  let sortKey = "task_id", sortDir = -1;    // -1 desc, 1 asc
  const selected = new Set();               // selected task_ids (persist across refresh)

  // How many of the newest tasks we ask for. This is a growing window, not a
  // page cursor: the server always returns "the newest N", so rows that arrive
  // while the user is reading never shift the window out from under them the
  // way an offset would. "Load more" widens N; polling reuses the same N so a
  // refresh never silently shrinks what is already on screen.
  const PAGE_STEP = 200;
  let windowSize = PAGE_STEP;
  let totalTasks = 0;
  // Mirrors task_service.MAX_LIST_LIMIT; used by history lookups that must not
  // be affected by how far the user has scrolled the list.
  const MAX_LOOKUP = 2000;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function toast(msg, ok) {
    const t = $("lm-toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3200);
  }

  // --- folder scanning (mirrors the legacy upload page) -------------------- //
  function stripTop(path) {
    const parts = path.split("/");
    return parts.length > 1 ? parts.slice(1).join("/") : path;
  }
  function scan(files) {
    entries = [];
    const ids = new Set();
    for (const file of files) {
      const full = file.webkitRelativePath || file.name;
      const rel = stripTop(full);
      entries.push({ file, full, rel });
      if (rel.endsWith("/judge.py")) ids.add(rel.slice(0, -"/judge.py".length));
      else if (rel === "judge.py") ids.add(full.split("/")[0]);
    }
    testIds = Array.from(ids).sort();
  }
  function scanAux(files) {
    const out = [];
    for (const file of files) out.push({ file, rel: file.webkitRelativePath || file.name });
    return out;
  }
  function checkboxes() {
    return Array.from(document.querySelectorAll("#lm-testids input[type=checkbox]"));
  }
  function selectedIds() { return checkboxes().filter((c) => c.checked).map((c) => c.value); }
  function updateCount() {
    $("lm-sel-count").textContent = `${selectedIds().length} / ${testIds.length} 已选`;
    refreshPreflight();
  }
  function renderTestIds() {
    const box = $("lm-testids");
    if (!testIds.length) {
      box.innerHTML = '<p class="lm-muted">该文件夹内未找到 judge.py。</p>';
      $("lm-submit").disabled = true;
      return;
    }
    $("lm-submit").disabled = false;
    box.innerHTML = testIds.map((id, i) =>
      `<label class="lm-check"><input type="checkbox" value="${esc(id)}" id="tc_${i}">
        <span>${esc(id)}</span></label>`).join("");
    checkboxes().forEach((c) => c.addEventListener("change", updateCount));
    updateCount();
  }
  function onFolder() {
    const files = $("lm-folder").files;
    if (!files || !files.length) return;
    scan(files);
    $("lm-scan-status").textContent =
      `发现 ${testIds.length} 个 test id（${(files[0].webkitRelativePath || "").split("/")[0]}）`;
    renderTestIds();
    $("lm-step2").hidden = false;
    $("lm-submit-result").innerHTML = "";
  }
  function filesToUpload(ids) {
    const prefixes = ids.map((id) => id + "/");
    const allIdPrefixes = testIds.map((id) => id + "/");
    const out = [];
    for (const e of entries) {
      const inSelected = prefixes.some((p) => e.rel.startsWith(p)) ||
        ids.includes(e.rel.replace(/\/judge\.py$/, ""));
      const inAnyCase = allIdPrefixes.some((p) => e.rel.startsWith(p));
      if (inSelected || !inAnyCase) out.push(e);
    }
    return out;
  }

  async function submit() {
    const ids = selectedIds();
    if (!ids.length) { $("lm-submit-status").textContent = "请至少勾选一个 test id。"; return; }
    const model = $("lm-model").value;
    if (!model) { $("lm-submit-status").textContent = "无可用模型，请联系系统管理员在管理台注册 .sil。"; return; }
    const chosen = filesToUpload(ids);
    const fd = new FormData();
    fd.append("model", model);
    fd.append("folder_name", (entries[0] && entries[0].full.split("/")[0]) || "folder");
    ids.forEach((id) => fd.append("test_ids", id));
    chosen.forEach((e) => { fd.append("files", e.file, e.full); fd.append("paths", e.full); });
    libEntries.forEach((e) => { fd.append("lib_files", e.file, e.rel); fd.append("lib_paths", e.rel); });
    stdlibEntries.forEach((e) => { fd.append("stdlib_files", e.file, e.rel); fd.append("stdlib_paths", e.rel); });

    $("lm-submit-status").textContent = `上传 ${chosen.length} 个文件（${ids.length} 个 test id）…`;
    $("lm-submit").disabled = true;
    try {
      const data = await LMApi.uploadProjectTree(pid, fd);
      renderResult(data);
      load();
    } catch (ex) {
      $("lm-submit-status").textContent = ex.message || "上传失败";
    } finally {
      $("lm-submit").disabled = false;
    }
  }
  function renderResult(data) {
    const created = data.created || [], dups = data.duplicates || [], errs = data.errors || [];
    $("lm-submit-status").textContent =
      `${created.length} 个已入队，${dups.length} 个重复，${errs.length} 个错误。`;
    const parts = [];
    created.forEach((c) => parts.push(`已入队 ${esc(c.task_id)} — ${esc(c.test_id)}`));
    dups.forEach((d) => parts.push(`已在队列：${esc(d.test_id)}（${esc(d.task_id)}）`));
    errs.forEach((e) => parts.push(`错误 ${esc(e.test_id)}：${esc(e.error)}`));
    (data.notes || []).forEach((n) => parts.push(`提示：${esc(n)}`));
    $("lm-submit-result").innerHTML = parts.map((p) => `<div>${p}</div>`).join("");
  }

  // --- task list ----------------------------------------------------------- //
  // Status vocabulary, verdict merging, time formatting and the row markup all
  // come from LMTaskRow (task_row.js). The workspace list renders the same rows
  // from the same module, so a change here cannot leave the two pages showing
  // the same task differently.
  const FINAL = LMTaskRow.FINAL;
  const STATUS_ZH = LMPill.TASK_ZH;
  const pill = LMPill.html;
  function statusBadge(s) { return pill(s, STATUS_ZH[s] || s); }

  const mergedBadge = LMTaskRow.mergedBadge;
  const fmtFinished = LMTaskRow.fmtFinished;

  function getFilters() {
    const st = $("lm-f-status"), sub = $("lm-f-submitter"), txt = $("lm-f-text");
    return {
      status: st ? st.value : "",
      submitter: sub ? sub.value.trim().toLowerCase() : "",
      text: txt ? txt.value.trim().toLowerCase() : "",
    };
  }
  // --- filters/sort <-> URL ------------------------------------------------ //
  // Filters use replaceState, not pushState: a filter is a refinement of the
  // current view, and pushing would add one history entry per keystroke, so
  // Back would walk the user letter-by-letter out of their own search.
  const DEFAULT_SORT = "task_id";
  const DEFAULT_DIR = -1;
  // A <select> cannot hold a value whose <option> does not exist yet, and the
  // submitter list is built from the task payload. Park the URL value here and
  // let populateSubmitters() apply it once the options are in the DOM.
  let pendingSubmitter = "";

  function syncFilterUrl() {
    if (!window.LMUrl) return;
    const st = $("lm-f-status"), sub = $("lm-f-submitter"), txt = $("lm-f-text");
    LMUrl.replace({
      status: st ? st.value : null,
      submitter: sub ? sub.value : null,
      // `q` rather than `text`: shorter, and matches the search convention
      // used by GitHub/Jira, so the URL stays readable when shared.
      q: txt ? txt.value.trim() : null,
      sort: sortKey === DEFAULT_SORT ? null : sortKey,
      dir: sortDir === DEFAULT_DIR ? null : (sortDir === 1 ? "asc" : "desc"),
    });
  }

  function applyUrlFilters() {
    if (!window.LMUrl) return;
    const q = LMUrl.all();
    const st = $("lm-f-status");
    if (st) st.value = q.status || "";
    // Keep the segmented control in step with the hidden input it feeds.
    const segEl = $("lm-f-seg");
    if (segEl) {
      segEl.querySelectorAll("button[data-status]").forEach((b) =>
        b.classList.toggle("on", (b.dataset.status || "") === (q.status || "")));
    }
    const txt = $("lm-f-text");
    if (txt) txt.value = q.q || "";
    const sub = $("lm-f-submitter");
    pendingSubmitter = q.submitter || "";
    if (sub) {
      // Only sticks if the option already exists; populateSubmitters() retries.
      sub.value = pendingSubmitter;
      if (sub.value === pendingSubmitter) pendingSubmitter = "";
    }
    sortKey = q.sort || DEFAULT_SORT;
    sortDir = q.dir === "asc" ? 1 : q.dir === "desc" ? -1 : DEFAULT_DIR;
  }

  // Fill the "全部提交者" dropdown from the loaded tasks, preserving any current pick.
  function populateSubmitters() {
    const sel = $("lm-f-submitter");
    if (!sel) return;
    const cur = sel.value || pendingSubmitter;
    pendingSubmitter = "";
    const names = Array.from(new Set(allTasks.map((t) => t.submitter).filter(Boolean))).sort();
    sel.innerHTML = '<option value="">全部提交者</option>' +
      names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    sel.value = names.includes(cur) ? cur : "";
  }
  const cmp = LMTaskRow.cmp;
  function visibleTasks() {
    const f = getFilters();
    const rows = allTasks.filter((t) => {
      if (f.status && t.status !== f.status) return false;
      if (f.submitter && !String(t.submitter || "").toLowerCase().includes(f.submitter)) return false;
      if (f.text) {
        const hay = (t.task_id + " " + (t.task_name || "") + " " + t.test_id).toLowerCase();
        if (!hay.includes(f.text)) return false;
      }
      return true;
    });
    rows.sort((a, b) => cmp(a, b, sortKey) * sortDir);
    return rows;
  }

  function renderTasks() {
    const rows = visibleTasks();
    const tb = $("lm-task-rows");
    if (!allTasks.length) {
      tb.innerHTML = '<tr><td colspan="9" class="lm-muted">暂无任务</td></tr>';
      $("lm-tasks-empty").hidden = true;
    } else if (!rows.length) {
      tb.innerHTML = "";
      $("lm-tasks-empty").hidden = false;
    } else {
      tb.innerHTML = rows.map((t) => rowHtml(t)).join("");
      $("lm-tasks-empty").hidden = true;
    }
    rowSig = {};                   // full re-render: drop the per-row patch cache
    allTasks.forEach((t) => { rowSig[t.task_id] = rowSignature(t); });
    ensureListPolling();           // keep running rows fresh WITHOUT a stream each
    bindRowActions();
    updateSortIndicators();
    updateBatchBar();
    renderMore();
  }

  /* Truncation notice. Only appears when the server actually clipped the list,
     so a project with 12 tasks never sees paging furniture it doesn't need. */
  function renderMore() {
    const bar = $("lm-tasks-more");
    if (!bar) return;
    const loaded = allTasks.length;
    const more = totalTasks > loaded;
    bar.hidden = !more;
    if (!more) return;
    const label = $("lm-tasks-count");
    // The filter count is stated separately: with a filter on, the visible row
    // count and the loaded count differ, and conflating them makes the number
    // look wrong to the user.
    const shown = visibleTasks().length;
    const filtered = shown !== loaded ? `（当前筛选显示 ${shown} 条）` : "";
    if (label) label.textContent = `已加载最近 ${loaded} / 共 ${totalTasks} 条${filtered}`;
    const btn = $("lm-tasks-more-btn");
    if (btn) btn.disabled = loaded >= MAX_LOOKUP;
    if (btn && loaded >= MAX_LOOKUP && label) {
      label.textContent = `已加载最近 ${loaded} / 共 ${totalTasks} 条，`
        + `已达单次上限，请用筛选缩小范围${filtered}`;
    }
  }
  // A task is "in the queue" only while queued/running; anything else (finished,
  // failed, error, cancelled) can be re-queued via 重新测试.
  const canRetest = LMTaskRow.canRetest;
  function rowHtml(t) {
    return LMTaskRow.rowHtml(t, {
      projectId: pid,
      canDelete: capabilities.delete,
      selected: selected.has(t.task_id),
    });
  }
  function bindRowActions() {
    document.querySelectorAll(".lm-task-cancel").forEach((b) =>
      b.addEventListener("click", () => cancelTask(b.dataset.k)));
    document.querySelectorAll(".lm-task-retest").forEach((b) =>
      b.addEventListener("click", () => retestTasks([b.dataset.k])));
    document.querySelectorAll(".lm-task-del").forEach((b) =>
      b.addEventListener("click", () => deleteTask(b.dataset.k)));
    document.querySelectorAll(".lm-task-view").forEach((b) =>
      b.addEventListener("click", () => openDetail(b.dataset.k)));
    document.querySelectorAll(".lm-task-steps").forEach((b) =>
      b.addEventListener("click", () => openSteps(b.dataset.tid)));
    document.querySelectorAll(".lm-task-open").forEach((a) =>
      a.addEventListener("click", (e) => { e.preventDefault(); openDetail(a.dataset.k); }));
    document.querySelectorAll(".lm-task-sel").forEach((cb) =>
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(cb.dataset.k); else selected.delete(cb.dataset.k);
        updateBatchBar();
      }));
  }

  function updateSortIndicators() {
    document.querySelectorAll(".lm-tasks-table thead th[data-sort]").forEach((th) => {
      const key = th.getAttribute("data-sort");
      const base = th.textContent.replace(/[ \u25b2\u25bc]+$/, "");
      th.textContent = key === sortKey ? base + (sortDir === 1 ? " \u25b2" : " \u25bc") : base;
      th.classList.toggle("lm-sorted", key === sortKey);
    });
  }
  function selectedKeys() {
    return Array.from(selected).filter((k) => allTasks.some((t) => t.task_id === k));
  }
  function updateBatchBar() {
    const n = selectedKeys().length;
    $("lm-batch-status").textContent = n ? `已选 ${n} 个` : "";
    const vis = visibleTasks();
    $("lm-check-all").checked = vis.length > 0 && vis.every((t) => selected.has(t.task_id));
  }

  function batchDownload() {
    const keys = selectedKeys().filter((k) => {
      const t = allTasks.find((x) => x.task_id === k);
      return t && t.has_result;
    });
    if (!keys.length) { toast("所选任务均无可下载的报告", false); return; }
    window.location = LMApi.projectTasksDownloadBatchUrl(pid, keys);
  }
  async function batchCancel() {
    const keys = selectedKeys().filter((k) => {
      const t = allTasks.find((x) => x.task_id === k);
      return t && !FINAL.includes(t.status);
    });
    if (!keys.length) { toast("所选任务没有可取消的运行", false); return; }
    if (!(await LMUI.confirm({
      level: "danger",
      title: `取消 ${keys.length} 个任务`,
      body: "所选运行中/排队中的任务将立即停止，已产生的部分结果会保留。",
      confirmText: "取消任务",
      cancelText: "返回",
    }))) return;
    let ok = 0;
    for (const k of keys) {
      try { await LMApi.cancelProjectTask(pid, k); ok++; } catch (ex) { /* keep going */ }
    }
    toast(`已请求取消 ${ok} 个任务`, true);
    load();
  }
  async function batchDelete() {
    const keys = selectedKeys();
    if (!keys.length) { toast("请先选择任务", false); return; }
    if (!(await LMUI.confirm({
      level: "critical",
      title: `删除 ${keys.length} 个任务`,
      body: "任务及其工作区与报告将一并删除，此操作不可撤销。",
      requireText: String(keys.length),
      confirmText: "永久删除",
    }))) return;
    try {
      const data = await LMApi.deleteProjectTasksBatch(pid, keys);
      const n = (data.results || []).filter((r) => r.result === "deleted").length;
      keys.forEach((k) => { selected.delete(k); closeStream(k); });
      toast(`${n} 个任务已删除`, true);
      load();
    } catch (ex) { toast(ex.message, false); }
  }
  async function retestTasks(keys) {
    const eligible = keys.filter((k) => {
      const t = allTasks.find((x) => x.task_id === k);
      return canRetest(t);
    });
    if (!eligible.length) { toast("所选任务均已在队列中，无需重新测试", false); return; }
    if (!(await LMUI.confirm({
      level: "danger",
      title: `重测 ${eligible.length} 个任务`,
      body: "任务将重新加入测试队列，原有结果会被覆盖。",
      confirmText: "重新测试",
    }))) return;
    try {
      const data = await LMApi.rerunSelectedTasks(pid, eligible);
      const n = (data.created || []).length;
      const skipped = (data.skipped || []).length;
      const missing = (data.missing || []).length;
      const errs = (data.errors || []).length;
      let msg = `已重新加入队列 ${n} 个任务`;
      const extra = [];
      if (skipped) extra.push(`跳过 ${skipped} 个（已在队列）`);
      if (missing) extra.push(`${missing} 个 test id 已失效`);
      if (errs) extra.push(`${errs} 个失败`);
      if (extra.length) msg += `，${extra.join("，")}`;
      toast(msg, n > 0);
      load();
    } catch (ex) { toast(ex.message, false); }
  }
  function batchRetest() { return retestTasks(selectedKeys()); }

  // Live progress for the whole LIST is delivered by ONE periodic poll of the
  // tasks endpoint — not one EventSource per running row. A browser caps
  // concurrent HTTP/1.1 connections per origin at ~6, so a batch run of many
  // rows used to exhaust the pool with long-lived streams; the detail modal's
  // own GET /detail + stream then could not connect, so "查看" showed no live
  // log or result. A single short poll leaves the pool free for the modal.
  const LIST_POLL_MS = 3000;
  let listTimer = null;
  let rowSig = {};   // task_id -> signature of the last painted row (skip no-op repaints)
  const rowSignature = LMTaskRow.signature;
  function anyRunning() {
    return allTasks.some((t) => !FINAL.includes(t.status));
  }
  function ensureListPolling() {
    if (anyRunning()) startListPolling();
    else stopListPolling();
  }
  function startListPolling() {
    if (listTimer) return;
    listTimer = setInterval(pollList, LIST_POLL_MS);
  }
  function stopListPolling() {
    if (listTimer) { clearInterval(listTimer); listTimer = null; }
  }
  // Refresh task rows in place. Repaints only the rows whose state actually
  // changed (status / progress / verdict / finish time / report availability),
  // so scroll position, checkbox selection and an open detail modal are all
  // preserved. Falls back to a full render when the row SET changes (a new task
  // appeared or one was removed elsewhere).
  async function pollList() {
    if (document.hidden) return;
    let data;
    try { data = await LMApi.listProjectTasks(pid, windowSize); }
    catch (_e) { return; }   // transient: keep the last snapshot, try again next tick
    const tasks = data.tasks || [];
    if (typeof data.total === "number") { totalTasks = data.total; renderMore(); }
    const oldIds = allTasks.map((t) => t.task_id).join(",");
    const newIds = tasks.map((t) => t.task_id).join(",");
    allTasks = tasks;
    const present = new Set(tasks.map((t) => t.task_id));
    Array.from(selected).forEach((k) => { if (!present.has(k)) selected.delete(k); });
    if (oldIds !== newIds) { renderTasks(); return; }   // structure changed → full render
    let patched = false;
    tasks.forEach((t) => {
      const sig = rowSignature(t);
      if (rowSig[t.task_id] === sig) return;
      rowSig[t.task_id] = sig;
      const row = document.querySelector(`tr[data-k="${cssEscape(t.task_id)}"]`);
      if (row) { row.outerHTML = rowHtml(t); patched = true; }
    });
    if (patched) { bindRowActions(); updateBatchBar(); }
    ensureListPolling();   // stop once every row reached a final state
  }
  // Retained for callers that used to tear down a per-row stream; streams are
  // gone, so this is a harmless no-op kept to avoid touching every call site.
  function closeStream() { /* no-op: list uses a single poll, not per-row SSE */ }
  function setCell(key, sel, val, html) {
    const row = document.querySelector(`tr[data-k="${cssEscape(key)}"]`);
    if (!row) return;
    const cell = row.querySelector(sel);
    if (!cell) return;
    if (html) cell.innerHTML = val; else cell.textContent = val;
  }
  async function refreshRow(key) {
    try {
      const data = await LMApi.projectTaskStatus(pid, key);
      const t = data.task;
      const idx = allTasks.findIndex((x) => x.task_id === key);
      if (idx >= 0 && t) allTasks[idx] = t;
      const row = document.querySelector(`tr[data-k="${cssEscape(key)}"]`);
      if (row && t) row.outerHTML = rowHtml(t);
      bindRowActions();
      updateBatchBar();
    } catch (e) { /* ignore */ }
  }
  function cssEscape(s) { return String(s).replace(/"/g, '\\"'); }

  async function cancelTask(key) {
    try { await LMApi.cancelProjectTask(pid, key); toast("已请求取消", true); refreshRow(key); }
    catch (ex) { toast(ex.message, false); }
  }
  async function deleteTask(key) {
    if (!(await LMUI.confirm({
      level: "danger",
      title: "删除该任务",
      body: `任务 ${key} 及其工作区与报告将被删除，此操作不可撤销。`,
      confirmText: "删除",
    }))) return;
    try {
      await LMApi.deleteProjectTask(pid, key);
      closeStream(key);
      toast("任务已删除", true);
      load();
    } catch (ex) { toast(ex.message, false); }
  }

  // --- task detail modal (live log + judge result) ------------------------ //
  const FAIL_LINE = /Step\.\d+\s+is\s+failed|Test\s+is\s+failed/i;
  const PASS_LINE = /Step\.\d+\s+is\s+passed|Test\s+is\s+Passed|All\s+steps\s+are\s+verified/i;
  const detailView = $("lm-task-detail");
  let detailKey = null;
  let detailStream = null;
  let judgeContent = "";

  // Task detail is an inline view (not a dialog): opening it hides the list
  // (its head + body) and reveals the detail section; closing reverses that.
  function showDetail(on) {
    if (root) root.hidden = on;
    if (detailView) detailView.hidden = !on;
    if (on) window.scrollTo(0, 0);
  }

  function classifyLine(line) {
    if (FAIL_LINE.test(line)) return "step-fail";
    if (PASS_LINE.test(line)) return "step-pass";
    return "";
  }
  function appendLog(text, cls) {
    const el = $("lm-d-log");
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text + "\n";
    el.appendChild(span);
    if ($("lm-d-autoscroll").checked) el.scrollTop = el.scrollHeight;
  }
  function setDetailStatus(status) {
    ["lm-d-status", "lm-d-hstatus"].forEach((id) => {
      const b = $(id);
      if (!b) return;
      b.className = "pill st-" + status;
      b.innerHTML = '<span class="dot"></span>' + esc(STATUS_ZH[status] || status);
    });
    $("lm-d-cancel").hidden = FINAL.includes(status);
  }
  function setDetailProgress(v) {
    $("lm-d-bar").style.width = v + "%";
    $("lm-d-progress").textContent = v + "%";
  }

  // --- live-log coalescing --------------------------------------------------
  // A busy task can emit dozens of SSE events per second. Writing each one to
  // the DOM immediately (createElement + appendChild + scrollTop) forces a
  // reflow per line and janks the log pane. Instead we buffer log lines and
  // keep only the latest progress/status, then flush everything once per frame:
  // a single DocumentFragment append + a single scroll. Progress/status are
  // idempotent "latest wins", so collapsing intermediate values loses nothing.
  let _logBuf = [];
  let _pendPct = null;
  let _pendStatus = null;
  let _flushHandle = 0;
  function _scheduleFlush() {
    if (_flushHandle) return;
    const run = () => { _flushHandle = 0; _flushDetail(); };
    _flushHandle = (typeof requestAnimationFrame === "function")
      ? requestAnimationFrame(run)
      : setTimeout(run, 16);
  }
  function _flushDetail() {
    if (_logBuf.length) {
      const el = $("lm-d-log");
      if (el) {
        const frag = document.createDocumentFragment();
        for (const it of _logBuf) {
          const span = document.createElement("span");
          if (it.cls) span.className = it.cls;
          span.textContent = it.text + "\n";
          frag.appendChild(span);
        }
        el.appendChild(frag);
        const as = $("lm-d-autoscroll");
        if (as && as.checked) el.scrollTop = el.scrollHeight;
      }
      _logBuf.length = 0;
    }
    if (_pendPct !== null) { setDetailProgress(_pendPct); _pendPct = null; }
    if (_pendStatus !== null) { setDetailStatus(_pendStatus); _pendStatus = null; }
  }
  function queueLog(text, cls) { _logBuf.push({ text: text, cls: cls || "" }); _scheduleFlush(); }
  function queueProgress(v) { _pendPct = v; _scheduleFlush(); }
  function queueStatus(s) { _pendStatus = s; _scheduleFlush(); }
  function resetDetailBuffers() {
    _logBuf.length = 0;
    _pendPct = null;
    _pendStatus = null;
    if (_flushHandle) {
      if (typeof requestAnimationFrame === "function") cancelAnimationFrame(_flushHandle);
      else clearTimeout(_flushHandle);
      _flushHandle = 0;
    }
  }

  // `push` is false when we are REACTING to history (popstate) or restoring on
  // first paint — writing to history there would either loop or bury the entry
  // the user is trying to go back to.
  /* Fill 概览 · 审核 with the sign-off of this run's verdict.
     The list column can only show the state; the reason a review was rejected
     is what the executor has to act on, so the detail panel shows it in full
     and links to the matrix row it belongs to. */
  function renderDetailReview(t) {
    const el = $("lm-d-review");
    if (!el) return;
    const r = t && t.review;
    if (!r || !r.status) {
      el.textContent = "—";
      el.title = "该判定按项目策略无需审核";
      return;
    }
    const bits = [];
    if (r.reviewer_name) bits.push("审核人 " + esc(r.reviewer_name));
    if (r.reviewed_at) bits.push(esc(r.reviewed_at.replace("T", " ").slice(0, 19)));
    const href = window.LMTaskRow ? LMTaskRow.reviewHref(t) : "";
    const badge = window.LMTaskRow
      ? LMTaskRow.reviewBadge(t)
      : esc(r.status);
    el.innerHTML = (href ? `<a href="${esc(href)}" class="lm-review-link">${badge}</a>`
                         : badge)
      + (bits.length ? ` <span class="submuted">${bits.join(" · ")}</span>` : "")
      + (r.note ? `<div class="submuted" style="margin-top:4px">${esc(r.note)}</div>` : "");
    el.title = r.note || "";
  }

  async function openDetail(key, opts) {
    if ((opts || {}).push !== false && window.LMUrl) LMUrl.set({ task: key });
    detailKey = key;
    resetDetailBuffers();
    $("lm-d-log").innerHTML = "";
    $("lm-d-judge").textContent = "";
    $("lm-d-judge-hint").textContent = "加载判定结果…";
    $("lm-d-download").href = LMApi.projectTaskDownloadUrl(pid, key);
    $("lm-d-title").textContent = key;
    $("lm-d-sub").textContent = "";
    showDetail(true);
    try {
      const data = await LMApi.projectTaskDetail(pid, key);
      const t = data.task;
      $("lm-d-testid").textContent = t.test_id || "";
      $("lm-d-submitter").textContent = t.submitter || "";
      $("lm-d-model").textContent = t.sil_name || t.sil_relpath || "";
      $("lm-d-verdict").textContent = t.result || "—";
      const created = (t.created_at || "").replace("T", " ").replace("Z", "");
      $("lm-d-created").textContent = created;
      $("lm-d-message").textContent = t.message || "";
      renderDetailReview(t);
      $("lm-d-sub").innerHTML = "test id <code>" + esc(t.test_id || "") + "</code>" +
        " · 任务 <code>" + esc(t.task_id || key) + "</code>" +
        (t.submitter ? " · 提交者 " + esc(t.submitter) : "") +
        (created ? " · " + esc(created) : "");
      setDetailStatus(t.status);
      setDetailProgress(t.progress || 0);
      $("lm-d-download").hidden = !t.has_result;
      (t.events || []).forEach((ev) => {
        if (ev.event_type === "warning") queueLog(ev.message, "warn");
        else if (ev.event_type === "error") queueLog(ev.message, "err");
        else if (ev.event_type === "log" || ev.event_type === "result") queueLog(ev.message);
      });
      if (!FINAL.includes(t.status)) startDetailStream(key);
    } catch (ex) {
      appendLog("加载详情失败：" + ex.message, "err");
    }
    loadJudge();
  }

  function startDetailStream(key) {
    closeDetailStream();
    const es = new EventSource(LMApi.projectTaskStreamUrl(pid, key));
    detailStream = es;
    const j = (e) => { try { return JSON.parse(e.data); } catch (_) { return {}; } };
    es.addEventListener("log", (e) => queueLog(j(e).message || ""));
    es.addEventListener("warning", (e) => queueLog(j(e).message || "", "warn"));
    es.addEventListener("error", (e) => { const d = j(e); if (d && d.message) queueLog(d.message, "err"); });
    es.addEventListener("progress", (e) => { const d = j(e); if (typeof d.value === "number") queueProgress(d.value); });
    es.addEventListener("status", (e) => { const d = j(e); if (d.status) queueStatus(d.status); });
    es.addEventListener("result", (e) => {
      const d = j(e);
      if (d.status) $("lm-d-verdict").textContent = d.status;
      if (d.message) { $("lm-d-message").textContent = d.message; queueLog(d.message); }
    });
    es.addEventListener("end", () => { _flushDetail(); closeDetailStream(); refreshDetailFinal(key); });
    es.onerror = () => { /* auto-retry */ };
  }
  function closeDetailStream() {
    _flushDetail();
    if (detailStream) { detailStream.close(); detailStream = null; }
  }
  async function refreshDetailFinal(key) {
    try {
      const data = await LMApi.projectTaskStatus(pid, key);
      const t = data.task;
      setDetailStatus(t.status);
      setDetailProgress(t.progress || 100);
      if (t.message) $("lm-d-message").textContent = t.message;
      $("lm-d-verdict").textContent = t.result || "—";
      $("lm-d-download").hidden = !t.has_result;
      refreshRow(key);
    } catch (e) { /* ignore */ }
    loadJudge();
  }

  // Strip a leading logging tag ("INFO:root:", "INFO Silver: ") so structural
  // markers can be matched regardless of how the judge tagged the line.
  function stripLogPrefix(s) {
    return s.replace(/^\s*(?:INFO|DEBUG|WARNING|WARN|ERROR|CRITICAL)(?::[^:]*:|\s+\S+:)\s?/i, "");
  }
  // The main step divider "-------------------Step3-------------------". It must
  // NOT match a subroutine sub-header
  // "------------------- Subroutine(Foo) Step3-------------------", so we anchor
  // "Step" right after the leading dashes (the subroutine form has a name there).
  const STEP_DIVIDER = /^-{3,}\s*Step\.?(\d+)\s*-{3,}$/i;
  // A *main* step verdict starts with "Step.N is …"; a subroutine verdict
  // ("<Subroutine> Step.2 is …") does not, so anchoring excludes it.
  const STEP_VERDICT = /^Step\.(\d+)\s+is\s+(passed|failed)/i;

  // Parse jdgrslt.log into per-step groups. A step is opened by its main divider
  // "-------------------StepN-------------------" (preferred) or, as a fallback,
  // by its "Step.N is passed/failed" verdict line. Every line up to the next
  // divider — the detail block (● / ▲ checks) and the verdict — attaches to that
  // step, so a step's block no longer bleeds into the previous one. A group's
  // result is fail if any of its lines matches FAIL_LINE, else pass if any
  // matches PASS_LINE. Lines before the first step are kept as a preamble.
  function parseJudgeSteps(text) {
    const steps = [], byNum = new Map(), preamble = [];
    let cur = null;
    const group = (n) => {
      if (byNum.has(n)) return byNum.get(n);
      const g = { n: n, lines: [], result: "" };
      byNum.set(n, g); steps.push(g); return g;
    };
    text.split(/\r?\n/).forEach((raw) => {
      const line = raw.replace(/\s+$/, "");
      if (line === "") return;
      const bare = stripLogPrefix(line);
      const md = bare.match(STEP_DIVIDER);
      if (md && !/Subroutine/i.test(bare)) {
        cur = group(md[1]);   // divider is structural — not part of the body
        return;
      }
      const mv = bare.match(STEP_VERDICT);
      if (mv) { cur = group(mv[1]); }   // fallback / result anchor for this step
      if (cur) {
        cur.lines.push(line);
        if (FAIL_LINE.test(line)) cur.result = "fail";
        else if (PASS_LINE.test(line) && cur.result !== "fail") cur.result = "pass";
      } else {
        preamble.push(line);
      }
    });
    return { steps: steps, preamble: preamble };
  }
  // Best-effort title for a step: the leading header lines that describe its
  // purpose — the category ("前提条件の確認") and the comment ("Step2 …") that
  // the runner logs right after the divider — joined together. Structural noise
  // (dividers, subroutine headers, ● / ▲ check markers, detail lines, verdict)
  // ends the header block.
  function stepTitle(s) {
    const noise = /^(?:-{3,}|[●▲]$|Monitoring target|Expected Value|Observed Value|確認タイミング|Step\.\d+\s+is\s+)/i;
    const parts = [];
    for (const raw of s.lines) {
      const t = stripLogPrefix(raw).trim();
      if (!t) continue;
      if (noise.test(t)) { if (parts.length) break; continue; }
      parts.push(t);
      if (parts.length >= 3) break;
    }
    return parts.length ? parts.join("　").slice(0, 120) : ("步骤 " + s.n);
  }
  // Render a step body, highlighting each failed check block (opened by "▲")
  // in red and dimming passed ones (opened by "●"). A block runs until the next
  // marker, divider or verdict line.
  function bodyHtml(lines) {
    let out = "", mode = "";
    lines.forEach((raw) => {
      const t = stripLogPrefix(raw).trim();
      if (t === "▲") mode = "fail";
      else if (t === "●") mode = "pass";
      else if (STEP_DIVIDER.test(t) || /^Step\.\d+\s+is\s+(passed|failed)/i.test(t)) mode = "";
      const cls = mode === "fail" ? "jck jck-fail" : (mode === "pass" ? "jck jck-pass" : "");
      out += cls ? `<span class="${cls}">${esc(raw)}</span>\n` : (esc(raw) + "\n");
    });
    return out;
  }
  function stepBadge(r) {
    if (r === "fail") return '<span class="jbadge fail">✕ failed</span>';
    if (r === "pass") return '<span class="jbadge pass">✓ passed</span>';
    return '<span class="jbadge none">— 待判定</span>';
  }

  function renderJudge() {
    const el = $("lm-d-judge");
    const failOnly = $("lm-d-failonly").checked;
    if (!judgeContent || !judgeContent.trim()) {
      el.innerHTML = '<div class="jempty">（空）</div>';
      return;
    }
    const parsed = parseJudgeSteps(judgeContent);
    // No recognizable steps → fall back to the raw log so nothing is hidden.
    if (!parsed.steps.length) {
      el.innerHTML = '<pre class="jraw"></pre>';
      el.querySelector(".jraw").textContent = judgeContent;
      return;
    }
    const rows = parsed.steps.filter((s) => !failOnly || s.result === "fail");
    if (!rows.length) {
      el.innerHTML = '<div class="jempty">无失败步骤。</div>';
      return;
    }
    el.innerHTML = rows.map((s) =>
      `<div class="jstep ${s.result || "none"}">
         <div class="jhead" role="button" tabindex="0" aria-expanded="false">
           <svg class="jchev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
           <span class="ji">Step.${esc(s.n)}</span>
           <span class="jt">${esc(stepTitle(s))}</span>
           ${stepBadge(s.result)}
         </div>
         <pre class="jbody" hidden>${bodyHtml(s.lines)}</pre>
       </div>`).join("");
    el.querySelectorAll(".jstep .jhead").forEach((h) => {
      const step = h.parentElement;
      const toggle = () => {
        const body = step.querySelector(".jbody");
        body.hidden = !body.hidden;
        step.classList.toggle("open", !body.hidden);
        h.setAttribute("aria-expanded", body.hidden ? "false" : "true");
      };
      h.addEventListener("click", toggle);
      h.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
    });
  }
  async function loadJudge() {
    if (!detailKey) return;
    const hint = $("lm-d-judge-hint");
    try {
      const data = await LMApi.projectTaskJdgrslt(pid, detailKey);
      if (!data.available) {
        judgeContent = "";
        hint.textContent = data.message || "暂无判定结果。";
        $("lm-d-judge").textContent = "";
        return;
      }
      judgeContent = data.content || "";
      const fails = typeof data.failed_steps === "number"
        ? data.failed_steps
        : judgeContent.split(/\r?\n/).filter((l) => classifyLine(l) === "step-fail").length;
      hint.innerHTML = "结论：" + esc(data.verdict || "—") +
        (fails ? ` · <span class="lm-err">${fails} 个失败步骤</span>` : "");
      renderJudge();
    } catch (ex) {
      hint.textContent = "判定结果不可用：" + ex.message;
    }
  }

  // --- 测试手顺直接编辑 ---------------------------------------------------- //
  // Reuses the matrix editor's graphical step-table drawer (LMStepsEditor,
  // steps_editor.js) so a task's 手順 can be edited *in place* from the task
  // list — the drawer slides up from the bottom exactly like on the editor page.
  // We locate the ``test`` sheet row whose ``test_id`` matches the task, hand it
  // to the drawer, and PATCH the ``steps`` cell on save through the normal
  // optimistic-locked item API. Rows are cached per page load and re-pulled on
  // refresh so concurrent edits are picked up.
  let testItemsCache = null;   // all ``test`` sheet rows, fetched once

  async function fetchTestItems() {
    if (testItemsCache) return testItemsCache;
    const acc = [];
    let p = 1;
    for (;;) {
      const data = await LMApi.listItems(pid, { page: p, page_size: 500, sheet: "test" });
      const batch = (data && data.items) || [];
      acc.push.apply(acc, batch);
      if (acc.length >= ((data && data.total) || 0) || batch.length === 0) break;
      p++;
      if (p > 2000) break;
    }
    testItemsCache = acc;
    return acc;
  }

  // Fetch all rows of a reference sheet (lib / const / io) for the drawer's
  // Lib/Const reference-search panel.
  async function fetchSheetItems(sheet) {
    const acc = [];
    let p = 1;
    for (;;) {
      const data = await LMApi.listItems(pid, { page: p, page_size: 500, sheet });
      const batch = (data && data.items) || [];
      acc.push.apply(acc, batch);
      if (acc.length >= ((data && data.total) || 0) || batch.length === 0) break;
      p++;
      if (p > 2000) break;
    }
    return acc;
  }

  async function openSteps(testId) {
    if (!window.LMStepsEditor) { toast("步骤编辑器未加载", false); return; }
    const tid = String(testId == null ? "" : testId).trim();
    if (!tid) { toast("该任务缺少 test id", false); return; }
    try {
      const items = await fetchTestItems();
      const row = items.find((it) => String(it.test_id == null ? "" : it.test_id).trim() === tid);
      if (!row) {
        toast(`在测试表中未找到 test id「${tid}」对应的行`, false);
        return;
      }
      LMStepsEditor.open(row, {
        fieldKey: "steps",
        testId: tid,
        onSave: async (json) => {
          const changes = { steps: json };
          const data = await LMApi.patchItem(pid, row.id, row.version, changes);
          const merged = data.item;
          row.version = merged.version;
          row.steps = merged.steps;
          toast("步骤明细已保存", true);
        },
        // Lib/Const/IO reference lookup for the drawer's reference panel.
        loadRef: async () => {
          const grab = async (sheet) => {
            try { return await fetchSheetItems(sheet); } catch (_e) { return []; }
          };
          const [lib, cst, io] = await Promise.all([grab("lib"), grab("const"), grab("io")]);
          return { lib, const: cst, io };
        },
        // Add a new io/const pool entry from the reference panel.
        addPool: async (sheet, values) => {
          const key = String(sheet) === "io" ? "io" : "const";
          await LMApi.addPoolEntry(pid, key, values);
          return true;
        },
        // Re-run this test id from the drawer's enqueue button.
        onEnqueue: async (id) => {
          const res = await LMApi.runSelectedTasks(pid, { test_ids: [id] });
          const errors = (res.errors || []);
          const missing = (res.missing || []);
          if (errors.length) throw new Error(errors[0].error || "入队失败");
          if (missing.length) throw new Error("未找到该 test_id 对应的测试行");
          toast(`已入队测试 ${id}`, true);
          load();
          return res;
        },
        getStatus: async (id) => {
          // Deliberately NOT windowSize: this searches history for one test id,
          // so a narrow window would report "never run" for a test whose last
          // run has scrolled past it. Asks for the server maximum instead.
          // (Proper fix is a dedicated per-test status endpoint -- see PLAN.md.)
          const res = await LMApi.listProjectTasks(pid, MAX_LOOKUP);
          const tasks = (res && res.tasks) || [];
          const mine = tasks.filter((t) => String(t.test_id) === String(id));
          if (!mine.length) return null;
          mine.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
          return mine[0].status || null;
        },
      });
    } catch (ex) {
      toast("加载手顺失败：" + (ex.message || "未知错误"), false);
    }
  }

  function closeDetail(opts) {
    closeDetailStream();
    detailKey = null;
    showDetail(false);
    if ((opts || {}).push !== false && window.LMUrl) LMUrl.set({ task: null });
  }
  // Deliberately NOT history.back(): if the user landed directly on ?task=…
  // (the whole point of a shareable link) back() would leave the site entirely.
  // Pushing the list view instead is always safe.
  $("lm-d-close").addEventListener("click", () => closeDetail());

  // Back/Forward moves between the list and whichever task was open, and also
  // restores that entry's filters — history entries carry the whole view, so
  // reinstating only the task would silently drop the user's search.
  if (window.LMUrl) {
    LMUrl.onPop((q) => {
      applyUrlFilters();
      renderTasks();
      const want = q.task || null;
      if (want && want !== detailKey) openDetail(want, { push: false });
      else if (!want && detailKey) closeDetail({ push: false });
    });
  }
  $("lm-d-refresh-judge").addEventListener("click", loadJudge);
  $("lm-d-failonly").addEventListener("change", renderJudge);
  $("lm-d-cancel").addEventListener("click", async () => {
    if (!detailKey) return;
    try { await LMApi.cancelProjectTask(pid, detailKey); toast("已请求取消", true); }
    catch (ex) { toast(ex.message, false); }
  });

  function renderModels(models) {
    const sel = $("lm-model");
    if (!models.length) {
      sel.innerHTML = '<option value="">（无已注册模型）</option>';
      return;
    }
    // Submit `name@version` rather than a bare name. A name alone means "run
    // whatever is registered right now", so a build that lands between opening
    // the page and pressing 提交 is attributed to the commit the user thought
    // they picked. The pinned ref makes that a 409 instead of wrong evidence.
    sel.innerHTML = models.map((m) => {
      const value = m.ref || m.name;
      const label = m.ref_short || m.version_short
        ? (m.ref_short || `${m.name}@${m.version_short}`)
        : m.name;
      return `<option value="${esc(value)}"${m.is_current ? " selected" : ""}>` +
        `${esc(label)}${m.is_current ? "（当前模型）" : ""}` +
        `${m.exists === false ? "（服务器缺失）" : ""}</option>`;
    }).join("");
    const cur = models.find((m) => m.is_current);
    if (cur) sel.value = cur.ref || cur.name;
  }
  function renderLicense(lic) {
    if (!lic) { $("lm-license").textContent = ""; return; }
    $("lm-license").textContent =
      `授权 ${lic.in_use || 0}/${lic.total || 0} 使用中，排队 ${lic.queued_jobs || 0}`;
  }

  async function load() {
    try {
      testItemsCache = null;   // refresh: re-pull steps so edits show on next view
      const data = await LMApi.listProjectTasks(pid, windowSize);
      totalTasks = typeof data.total === "number" ? data.total : (data.tasks || []).length;
      lastModels = data.models || [];
      lastLicense = data.license || null;
      lastRole = data.role || "";
      renderModels(lastModels);
      renderLicense(lastLicense);
      refreshPreflight();
      capabilities.delete = !!data.can_delete;
      $("lm-batch-delete").hidden = !capabilities.delete;
      Object.keys(streams).forEach(closeStream);
      allTasks = data.tasks || [];
      const present = new Set(allTasks.map((t) => t.task_id));
      Array.from(selected).forEach((k) => { if (!present.has(k)) selected.delete(k); });
      populateSubmitters();
      renderTasks();
      $("lm-tasks-body").hidden = false;
      $("lm-tasks-denied").hidden = true;
      const acts = $("lm-tasks-actions"); if (acts) acts.hidden = false;
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      if (ex.status === 403) {
        $("lm-tasks-denied").hidden = false;
        $("lm-tasks-body").hidden = true;
        const acts = $("lm-tasks-actions"); if (acts) acts.hidden = true;
        return;
      }
      toast(ex.message, false);
    }
  }

  // --- submit preflight ----------------------------------------------------- //
  // Every precondition used to be discovered *after* the work: the "no model
  // registered" case only appeared at the submit click, once the user had
  // already picked a folder and ticked test ids. LMPreflight decides what to
  // say; this block only collects state and re-runs it on every input.
  let pfVocab = null;        // {stdlib, silver_roots} — fetched once, may stay null
  let pfMissing = [];        // module names no uploaded file can satisfy
  let pfScanned = false;     // have we actually read the judge sources yet?

  function preflightState() {
    return {
      models: lastModels,
      selectedModel: ($("lm-model") || {}).value || "",
      license: lastLicense,
      role: lastRole,
      folderChosen: entries.length > 0,
      testIdCount: testIds.length,
      selectedCount: selectedIds().length,
      libChosen: libEntries.length > 0 || stdlibEntries.length > 0,
      scanned: pfScanned,
      missingModules: pfMissing
    };
  }
  function refreshPreflight() {
    const host = $("lm-preflight");
    if (!host || !window.LMPreflight) return;
    window.LMPreflight.render(host, window.LMPreflight.evaluate(preflightState()));
  }

  function readText(file) {
    return new Promise((resolve) => {
      try {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result || ""));
        r.onerror = () => resolve("");
        r.readAsText(file);
      } catch (_e) { resolve(""); }
    });
  }
  /* Read the judge sources and ask LMPreflight which imports nothing in the
   * upload can satisfy. Without the server's module vocabulary we skip the
   * check entirely rather than guess: a wrong "missing module" warning is worse
   * than no warning, because it trains people to dismiss the bar. */
  async function rescanImports() {
    if (!pfVocab || !window.LMPreflight) { pfScanned = false; pfMissing = []; return; }
    const judges = entries.filter((e) => /(^|\/)judge\.py$/.test(e.rel));
    if (!judges.length) { pfScanned = false; pfMissing = []; return; }
    const texts = await Promise.all(judges.slice(0, 200).map((e) => readText(e.file)));
    const paths = entries.map((e) => e.rel)
      .concat(libEntries.map((e) => e.rel), stdlibEntries.map((e) => e.rel));
    pfMissing = window.LMPreflight.unresolvedImports(
      texts, paths, pfVocab.stdlib, pfVocab.silver_roots);
    pfScanned = true;
  }
  async function rescanAndRender() {
    try { await rescanImports(); } catch (_e) { pfScanned = false; pfMissing = []; }
    refreshPreflight();
  }

  $("lm-folder").addEventListener("change", () => { onFolder(); rescanAndRender(); });
  $("lm-lib").addEventListener("change", () => {
    libEntries = scanAux($("lm-lib").files); rescanAndRender();
  });
  $("lm-stdlib").addEventListener("change", () => {
    stdlibEntries = scanAux($("lm-stdlib").files); rescanAndRender();
  });
  const pfModelSel = $("lm-model");
  if (pfModelSel) pfModelSel.addEventListener("change", refreshPreflight);
  if (window.LMApi && typeof LMApi.taskPreflight === "function") {
    LMApi.taskPreflight(pid).then((d) => {
      pfVocab = { stdlib: d.stdlib || [], silver_roots: d.silver_roots || [] };
      return rescanAndRender();
    }).catch(() => { /* older server or no permission: bar still shows the rest */ });
  }
  $("lm-submit").addEventListener("click", submit);
  $("lm-sel-all").addEventListener("click", () => { checkboxes().forEach((c) => (c.checked = true)); updateCount(); });
  $("lm-sel-none").addEventListener("click", () => { checkboxes().forEach((c) => (c.checked = false)); updateCount(); });
  $("lm-refresh").addEventListener("click", load);

  // Upload is an inline collapsible panel (mirrors 模型管理's 添加模型 form), not a
  // modal dialog — the header button toggles it open/closed. Guard every node:
  // if a cached/older template is served these must not throw and abort the rest
  // of init (filters, sorting, list load) — that would break the page.
  const uploadPanel = $("lm-upload-panel");
  const openUpload = $("lm-open-upload");
  if (openUpload && uploadPanel) {
    openUpload.addEventListener("click", () => {
      uploadPanel.hidden = !uploadPanel.hidden;
      openUpload.classList.toggle("active", !uploadPanel.hidden);
      if (!uploadPanel.hidden) {
        uploadPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
        const m = $("lm-model"); if (m) m.focus();
      }
    });
  }

  // Filter + sort + batch selection.
  // renderFromUser = "the user changed something", i.e. also mirror it to the
  // URL. Internal re-renders (polling, row patches, load) call renderTasks
  // directly so they never touch history.
  function renderFromUser() { syncFilterUrl(); renderTasks(); }

  ["lm-f-submitter", "lm-f-text"].forEach((id) => {
    const el = $(id); if (el) el.addEventListener("input", renderFromUser);
  });
  // Segmented status tabs write the (hidden) lm-f-status value read by getFilters.
  const seg = $("lm-f-seg");
  if (seg) {
    seg.querySelectorAll("button[data-status]").forEach((b) => {
      b.addEventListener("click", () => {
        seg.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        const st = $("lm-f-status"); if (st) st.value = b.dataset.status || "";
        renderFromUser();
      });
    });
  }
  $("lm-check-all").addEventListener("change", () => {
    const on = $("lm-check-all").checked;
    visibleTasks().forEach((t) => { if (on) selected.add(t.task_id); else selected.delete(t.task_id); });
    renderTasks();
  });
  const moreBtn = $("lm-tasks-more-btn");
  if (moreBtn) {
    moreBtn.addEventListener("click", async () => {
      moreBtn.disabled = true;
      const prev = moreBtn.textContent;
      moreBtn.textContent = "加载中…";
      windowSize = Math.min(windowSize + PAGE_STEP, MAX_LOOKUP);
      try {
        await load();
      } finally {
        // renderMore() owns `disabled` from here; only the label is restored.
        moreBtn.textContent = prev;
        renderMore();
      }
    });
  }
  $("lm-batch-download").addEventListener("click", batchDownload);
  $("lm-batch-cancel").addEventListener("click", batchCancel);
  $("lm-batch-retest").addEventListener("click", batchRetest);
  $("lm-batch-delete").addEventListener("click", batchDelete);
  document.querySelectorAll(".lm-tasks-table thead th[data-sort]").forEach((th) => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const key = th.getAttribute("data-sort");
      // Same rule as the workspace list: keys whose interesting end is the high
      // one start descending (待审核 before 已通过, latest finish first).
      if (key === sortKey) sortDir = -sortDir;
      else {
        sortKey = key;
        sortDir = (key === "task_id" || key === "finished_at" || key === "review")
          ? -1 : 1;
      }
      renderFromUser();
    });
  });

  window.addEventListener("beforeunload", () => Object.keys(streams).forEach(closeStream));

  // Reveal the return path only for users who actually came from the workspace;
  // for everyone else the link would point somewhere they never were.
  (function wireBackToWorkspace() {
    const back = document.getElementById("lm-back-workspace");
    if (!back || !window.LMTaskActions) return;
    if (!LMTaskActions.cameFromWorkspace()) return;
    back.href = LMTaskActions.backUrl();
    back.hidden = false;
  })();

  // Read the URL BEFORE the first fetch so load()'s render already reflects the
  // linked-to filters — no flash of the unfiltered list.
  applyUrlFilters();

  (window.LMReady || Promise.resolve()).then(async () => {
    await load();
    // Detail is self-contained (it fetches its own payload), so it can open as
    // soon as the list settles. Restoring without pushing keeps the entry the
    // user actually arrived on at the top of the stack.
    const deepTask = window.LMUrl ? LMUrl.get("task") : "";
    if (deepTask) openDetail(deepTask, { push: false });
  });
})();
