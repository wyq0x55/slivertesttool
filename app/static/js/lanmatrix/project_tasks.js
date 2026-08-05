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
  const streams = {};   // task_id -> EventSource
  let allTasks = [];                        // latest snapshot from the server
  let sortKey = "task_id", sortDir = -1;    // -1 desc, 1 asc
  const selected = new Set();               // selected task_ids (persist across refresh)

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
  const FINAL = ["passed", "failed", "cancelled"];
  const STATUS_ZH = { queued: "排队中", running: "运行中", passed: "通过",
    failed: "失败", error: "异常", cancelled: "已取消", notask: "—" };
  function pill(cls, label, tip) {
    const c = cls || "notask";
    return `<span class="pill st-${esc(c)}" title="${esc(tip || label)}"><span class="dot"></span>${esc(label)}</span>`;
  }
  function statusBadge(s) { return pill(s, STATUS_ZH[s] || s); }

  // Merge the execution ``status`` and the judge ``result`` (verdict) into a
  // single label. A finished-but-failing run carries status ``failed``; we
  // split that into a genuine test ``failed`` (verdict FAIL) versus an
  // execution/judge ``error`` (verdict ERROR) so the two are distinguishable.
  function mergedVerdict(t) {
    const st = String(t.status || "").toLowerCase();
    if (st === "failed") {
      const v = String(t.result || "").trim().toUpperCase();
      if (v.startsWith("ERROR")) return { cls: "error", label: "error" };
      return { cls: "failed", label: "failed" };
    }
    if (st === "passed" || st === "cancelled" || st === "running" || st === "queued") {
      return { cls: st, label: st };
    }
    return { cls: st || "notask", label: st || "—" };
  }
  function mergedBadge(t) {
    const m = mergedVerdict(t);
    const tip = String(t.result || t.status || "");
    return pill(m.cls, STATUS_ZH[m.label] || m.label, tip);
  }

  // Format a task's completion moment (``finished_at``, an ISO UTC string) as
  // ``YY/MM/DD HH:MM:SS`` in the viewer's local time, e.g. ``26/07/20 11:18:15``.
  function fmtFinished(t) {
    const iso = t.finished_at;
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getFullYear() % 100)}/${p(d.getMonth() + 1)}/${p(d.getDate())} `
      + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function getFilters() {
    const st = $("lm-f-status"), sub = $("lm-f-submitter"), txt = $("lm-f-text");
    return {
      status: st ? st.value : "",
      submitter: sub ? sub.value.trim().toLowerCase() : "",
      text: txt ? txt.value.trim().toLowerCase() : "",
    };
  }
  // Fill the "全部提交者" dropdown from the loaded tasks, preserving any current pick.
  function populateSubmitters() {
    const sel = $("lm-f-submitter");
    if (!sel) return;
    const cur = sel.value;
    const names = Array.from(new Set(allTasks.map((t) => t.submitter).filter(Boolean))).sort();
    sel.innerHTML = '<option value="">全部提交者</option>' +
      names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    sel.value = names.includes(cur) ? cur : "";
  }
  function cmp(a, b, key) {
    if (key === "progress") return (+a.progress || 0) - (+b.progress || 0);
    const x = String(a[key] == null ? "" : a[key]).toLowerCase();
    const y = String(b[key] == null ? "" : b[key]).toLowerCase();
    return x < y ? -1 : x > y ? 1 : 0;
  }
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
  }
  function rowHtml(t) {
    const checked = selected.has(t.task_id) ? " checked" : "";
    const p = t.progress || 0;
    const sel = `<td class="lm-col-check"><input type="checkbox" class="lm-task-sel" data-k="${esc(t.task_id)}"${checked}></td>`;
    const view = `<button class="btn small lm-task-view" data-k="${esc(t.task_id)}">查看</button>`;
    const steps = t.test_id
      ? `<button class="btn small lm-task-steps" data-k="${esc(t.task_id)}" data-tid="${esc(t.test_id)}">手顺</button>` : "";
    const dl = t.has_result
      ? `<a class="btn small" href="${LMApi.projectTaskDownloadUrl(pid, t.task_id)}">下载</a>` : "";
    const cancel = FINAL.includes(t.status)
      ? "" : `<button class="btn small lm-task-cancel" data-k="${esc(t.task_id)}">取消</button>`;
    const del = capabilities.delete
      ? `<button class="btn small danger lm-task-del" data-k="${esc(t.task_id)}">删除</button>` : "";
    return `<tr data-k="${esc(t.task_id)}">
      ${sel}
      <td><a href="#" class="lm-task-open" data-k="${esc(t.task_id)}"><code>${esc(t.task_id)}</code></a></td>
      <td>${esc(t.test_id)}</td>
      <td>${esc(t.sil_name || "")}</td>
      <td>${esc(t.submitter)}</td>
      <td class="lm-cell-status">${mergedBadge(t)}</td>
      <td class="lm-cell-progress">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="prog" style="width:70px;flex:0 0 auto"><i style="width:${p}%"></i></div>
          <span class="muted" style="font-size:12px">${p}%</span>
        </div>
      </td>
      <td class="lm-cell-time"><code>${esc(fmtFinished(t))}</code></td>
      <td class="lm-row-actions"><span class="row-acts" style="display:flex;gap:4px;justify-content:flex-end">${view} ${steps} ${cancel} ${dl} ${del}</span></td>
    </tr>`;
  }
  function bindRowActions() {
    document.querySelectorAll(".lm-task-cancel").forEach((b) =>
      b.addEventListener("click", () => cancelTask(b.dataset.k)));
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
    if (!confirm(`确定取消所选 ${keys.length} 个运行中/排队中的任务？`)) return;
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
    if (!confirm(`确定删除所选 ${keys.length} 个任务及其工作区与报告？此操作不可撤销。`)) return;
    try {
      const data = await LMApi.deleteProjectTasksBatch(pid, keys);
      const n = (data.results || []).filter((r) => r.result === "deleted").length;
      keys.forEach((k) => { selected.delete(k); closeStream(k); });
      toast(`${n} 个任务已删除`, true);
      load();
    } catch (ex) { toast(ex.message, false); }
  }

  // Live progress for the whole LIST is delivered by ONE periodic poll of the
  // tasks endpoint — not one EventSource per running row. A browser caps
  // concurrent HTTP/1.1 connections per origin at ~6, so a batch run of many
  // rows used to exhaust the pool with long-lived streams; the detail modal's
  // own GET /detail + stream then could not connect, so "查看" showed no live
  // log or result. A single short poll leaves the pool free for the modal.
  const LIST_POLL_MS = 3000;
  let listTimer = null;
  let rowSig = {};   // task_id -> signature of the last painted row (skip no-op repaints)
  function rowSignature(t) {
    return [t.status, t.progress || 0, t.has_result ? 1 : 0,
            t.result || "", fmtFinished(t)].join("|");
  }
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
    try { data = await LMApi.listProjectTasks(pid); }
    catch (_e) { return; }   // transient: keep the last snapshot, try again next tick
    const tasks = data.tasks || [];
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
    if (!confirm("确定删除该任务及其工作区？")) return;
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

  async function openDetail(key) {
    detailKey = key;
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
      $("lm-d-sub").innerHTML = "test id <code>" + esc(t.test_id || "") + "</code>" +
        (t.submitter ? " · 提交者 " + esc(t.submitter) : "") +
        (created ? " · " + esc(created) : "");
      setDetailStatus(t.status);
      setDetailProgress(t.progress || 0);
      $("lm-d-download").hidden = !t.has_result;
      (t.events || []).forEach((ev) => {
        if (ev.event_type === "warning") appendLog(ev.message, "warn");
        else if (ev.event_type === "error") appendLog(ev.message, "err");
        else if (ev.event_type === "log" || ev.event_type === "result") appendLog(ev.message);
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
    es.addEventListener("log", (e) => appendLog(j(e).message || ""));
    es.addEventListener("warning", (e) => appendLog(j(e).message || "", "warn"));
    es.addEventListener("error", (e) => { const d = j(e); if (d && d.message) appendLog(d.message, "err"); });
    es.addEventListener("progress", (e) => { const d = j(e); if (typeof d.value === "number") setDetailProgress(d.value); });
    es.addEventListener("status", (e) => { const d = j(e); if (d.status) setDetailStatus(d.status); });
    es.addEventListener("result", (e) => {
      const d = j(e);
      if (d.status) $("lm-d-verdict").textContent = d.status;
      if (d.message) { $("lm-d-message").textContent = d.message; appendLog(d.message); }
    });
    es.addEventListener("end", () => { closeDetailStream(); refreshDetailFinal(key); });
    es.onerror = () => { /* auto-retry */ };
  }
  function closeDetailStream() {
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

  // Parse jdgrslt.log into per-step groups. Each "Step.N" line opens a group;
  // subsequent non-step lines (continuations / detail) attach to it. A group's
  // verdict is fail if any of its lines matches FAIL_LINE, else pass if any
  // matches PASS_LINE. Lines before the first Step.N are kept as a preamble.
  function parseJudgeSteps(text) {
    const steps = [], byNum = new Map(), preamble = [];
    let cur = null;
    text.split(/\r?\n/).forEach((raw) => {
      const line = raw.replace(/\s+$/, "");
      if (line === "") return;
      const m = line.match(/Step\.(\d+)/i);
      if (m) {
        const n = m[1];
        if (byNum.has(n)) { cur = byNum.get(n); }
        else { cur = { n: n, lines: [], result: "" }; byNum.set(n, cur); steps.push(cur); }
      }
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
  // Best-effort short title for a step: first line, minus the "Step.N" prefix
  // and any trailing "is passed/failed" verdict.
  function stepTitle(s) {
    let t = s.lines[0] || ("Step." + s.n);
    t = t.replace(/^\s*Step\.\d+\s*[:：.\-)]*\s*/i, "");
    t = t.replace(/\b(is\s+)?(passed|failed)\b\.?\s*$/i, "").trim();
    return t || ("步骤 " + s.n);
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
         <pre class="jbody" hidden>${esc(s.lines.join("\n"))}</pre>
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
        toast(`在测试矩阵中未找到 test id「${tid}」对应的行`, false);
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
          const res = await LMApi.listProjectTasks(pid);
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

  function closeDetail() {
    closeDetailStream();
    detailKey = null;
    showDetail(false);
  }
  $("lm-d-close").addEventListener("click", closeDetail);
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
    sel.innerHTML = models.map((m) =>
      `<option value="${esc(m.name)}">${esc(m.name)}${m.exists === false ? "（服务器缺失）" : ""}</option>`).join("");
  }
  function renderLicense(lic) {
    if (!lic) { $("lm-license").textContent = ""; return; }
    $("lm-license").textContent =
      `授权 ${lic.in_use || 0}/${lic.total || 0} 使用中，排队 ${lic.queued_jobs || 0}`;
  }

  async function load() {
    try {
      testItemsCache = null;   // refresh: re-pull steps so edits show on next view
      const data = await LMApi.listProjectTasks(pid);
      renderModels(data.models || []);
      renderLicense(data.license);
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

  $("lm-folder").addEventListener("change", onFolder);
  $("lm-lib").addEventListener("change", () => { libEntries = scanAux($("lm-lib").files); });
  $("lm-stdlib").addEventListener("change", () => { stdlibEntries = scanAux($("lm-stdlib").files); });
  $("lm-submit").addEventListener("click", submit);
  $("lm-sel-all").addEventListener("click", () => { checkboxes().forEach((c) => (c.checked = true)); updateCount(); });
  $("lm-sel-none").addEventListener("click", () => { checkboxes().forEach((c) => (c.checked = false)); updateCount(); });
  $("lm-refresh").addEventListener("click", load);

  // Upload is now a modal dialog opened from the task-list header. Guard every
  // node: if a cached/older template is served these must not throw and abort
  // the rest of init (filters, sorting, list load) — that would break the page.
  const uploadDlg = $("lm-upload-dialog");
  const openUpload = $("lm-open-upload");
  const closeUpload = $("lm-upload-close");
  if (openUpload && uploadDlg) {
    openUpload.addEventListener("click", () => {
      if (uploadDlg.showModal) uploadDlg.showModal(); else uploadDlg.setAttribute("open", "");
    });
  }
  if (closeUpload && uploadDlg) {
    closeUpload.addEventListener("click", () => {
      if (uploadDlg.close) uploadDlg.close(); else uploadDlg.removeAttribute("open");
    });
  }

  // Filter + sort + batch selection.
  ["lm-f-submitter", "lm-f-text"].forEach((id) => {
    const el = $(id); if (el) el.addEventListener("input", renderTasks);
  });
  // Segmented status tabs write the (hidden) lm-f-status value read by getFilters.
  const seg = $("lm-f-seg");
  if (seg) {
    seg.querySelectorAll("button[data-status]").forEach((b) => {
      b.addEventListener("click", () => {
        seg.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        const st = $("lm-f-status"); if (st) st.value = b.dataset.status || "";
        renderTasks();
      });
    });
  }
  $("lm-check-all").addEventListener("change", () => {
    const on = $("lm-check-all").checked;
    visibleTasks().forEach((t) => { if (on) selected.add(t.task_id); else selected.delete(t.task_id); });
    renderTasks();
  });
  $("lm-batch-download").addEventListener("click", batchDownload);
  $("lm-batch-cancel").addEventListener("click", batchCancel);
  $("lm-batch-delete").addEventListener("click", batchDelete);
  document.querySelectorAll(".lm-tasks-table thead th[data-sort]").forEach((th) => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const key = th.getAttribute("data-sort");
      if (key === sortKey) sortDir = -sortDir; else { sortKey = key; sortDir = 1; }
      renderTasks();
    });
  });

  window.addEventListener("beforeunload", () => Object.keys(streams).forEach(closeStream));

  (window.LMReady || Promise.resolve()).then(load);
})();
