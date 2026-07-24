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
  function statusBadge(s) { return `<span class="lm-badge lm-status-${esc(s)}">${esc(s)}</span>`; }

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
    return `<span class="lm-badge lm-status-${esc(m.cls)}" title="${esc(tip)}">${esc(m.label)}</span>`;
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
    return {
      status: $("lm-f-status").value,
      submitter: $("lm-f-submitter").value.trim().toLowerCase(),
      text: $("lm-f-text").value.trim().toLowerCase(),
    };
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
    allTasks.forEach((t) => { if (!FINAL.includes(t.status)) openStream(t.task_id); });
    bindRowActions();
    updateSortIndicators();
    updateBatchBar();
  }
  function rowHtml(t) {
    const checked = selected.has(t.task_id) ? " checked" : "";
    const sel = `<td class="lm-col-check"><input type="checkbox" class="lm-task-sel" data-k="${esc(t.task_id)}"${checked}></td>`;
    const view = `<button class="lm-btn lm-btn-sm lm-task-view" data-k="${esc(t.task_id)}">查看</button>`;
    const dl = t.has_result
      ? `<a class="lm-btn lm-btn-sm" href="${LMApi.projectTaskDownloadUrl(pid, t.task_id)}">下载</a>` : "";
    const cancel = FINAL.includes(t.status)
      ? "" : `<button class="lm-btn lm-btn-sm lm-task-cancel" data-k="${esc(t.task_id)}">取消</button>`;
    const del = capabilities.delete
      ? `<button class="lm-btn lm-btn-sm lm-btn-danger lm-task-del" data-k="${esc(t.task_id)}">删除</button>` : "";
    return `<tr data-k="${esc(t.task_id)}">
      ${sel}
      <td><a href="#" class="lm-task-open" data-k="${esc(t.task_id)}"><code>${esc(t.task_id)}</code></a></td>
      <td>${esc(t.test_id)}</td>
      <td>${esc(t.sil_name || "")}</td>
      <td>${esc(t.submitter)}</td>
      <td class="lm-cell-status">${mergedBadge(t)}</td>
      <td class="lm-cell-progress">${t.progress || 0}%</td>
      <td class="lm-cell-time"><code>${esc(fmtFinished(t))}</code></td>
      <td class="lm-row-actions">${view} ${cancel} ${dl} ${del}</td>
    </tr>`;
  }
  function bindRowActions() {
    document.querySelectorAll(".lm-task-cancel").forEach((b) =>
      b.addEventListener("click", () => cancelTask(b.dataset.k)));
    document.querySelectorAll(".lm-task-del").forEach((b) =>
      b.addEventListener("click", () => deleteTask(b.dataset.k)));
    document.querySelectorAll(".lm-task-view").forEach((b) =>
      b.addEventListener("click", () => openDetail(b.dataset.k)));
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

  function openStream(key) {
    if (streams[key]) return;
    const es = new EventSource(LMApi.projectTaskStreamUrl(pid, key));
    streams[key] = es;
    const parse = (ev) => { try { return JSON.parse(ev.data); } catch (e) { return null; } };
    es.addEventListener("progress", (ev) => {
      const d = parse(ev);
      if (d && typeof d.value === "number") setCell(key, ".lm-cell-progress", d.value + "%");
    });
    es.addEventListener("status", (ev) => {
      const d = parse(ev);
      // In-flight updates (queued/running) show the plain lifecycle status; the
      // final merged verdict (passed/failed/error) is rendered by refreshRow on
      // ``end`` once the judge result is known.
      if (d && d.status) setCell(key, ".lm-cell-status", statusBadge(d.status), true);
    });
    es.addEventListener("end", () => { closeStream(key); refreshRow(key); });
    es.onerror = () => { closeStream(key); };
  }
  function closeStream(key) {
    if (streams[key]) { streams[key].close(); delete streams[key]; }
  }
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
  const dlg = $("lm-task-dialog");
  let detailKey = null;
  let detailStream = null;
  let judgeContent = "";

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
    const b = $("lm-d-status");
    b.textContent = status;
    b.className = "lm-badge lm-status-" + status;
    const isFinal = FINAL.includes(status);
    $("lm-d-cancel").hidden = isFinal;
  }
  function setDetailProgress(v) {
    $("lm-d-bar").style.width = v + "%";
    $("lm-d-progress").textContent = v;
  }

  async function openDetail(key) {
    detailKey = key;
    $("lm-d-log").innerHTML = "";
    $("lm-d-judge").textContent = "";
    $("lm-d-judge-hint").textContent = "加载判定结果…";
    $("lm-d-download").href = LMApi.projectTaskDownloadUrl(pid, key);
    $("lm-d-title").textContent = "任务 " + key;
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    try {
      const data = await LMApi.projectTaskDetail(pid, key);
      const t = data.task;
      $("lm-d-testid").textContent = t.test_id || "";
      $("lm-d-submitter").textContent = t.submitter || "";
      $("lm-d-model").textContent = t.sil_name || t.sil_relpath || "";
      $("lm-d-verdict").textContent = t.result || "—";
      $("lm-d-created").textContent = (t.created_at || "").replace("T", " ").replace("Z", "");
      $("lm-d-message").textContent = t.message || "";
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

  function renderJudge() {
    const el = $("lm-d-judge");
    el.innerHTML = "";
    const failOnly = $("lm-d-failonly").checked;
    let shown = 0;
    judgeContent.split(/\r?\n/).forEach((line) => {
      if (line === "") return;
      const cls = classifyLine(line);
      if (failOnly && cls !== "step-fail") return;
      const span = document.createElement("span");
      if (cls) span.className = cls;
      span.textContent = line + "\n";
      el.appendChild(span);
      shown += 1;
    });
    if (!shown) el.textContent = failOnly ? "无失败步骤。" : "（空）";
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

  function closeDetail() {
    closeDetailStream();
    detailKey = null;
    if (dlg.open) dlg.close();
  }
  $("lm-d-close").addEventListener("click", closeDetail);
  dlg.addEventListener("close", closeDetailStream);
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
      const data = await LMApi.listProjectTasks(pid);
      renderModels(data.models || []);
      renderLicense(data.license);
      capabilities.delete = !!data.can_delete;
      $("lm-batch-delete").hidden = !capabilities.delete;
      Object.keys(streams).forEach(closeStream);
      allTasks = data.tasks || [];
      const present = new Set(allTasks.map((t) => t.task_id));
      Array.from(selected).forEach((k) => { if (!present.has(k)) selected.delete(k); });
      renderTasks();
      $("lm-tasks-body").hidden = false;
      $("lm-tasks-denied").hidden = true;
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      if (ex.status === 403) {
        $("lm-tasks-denied").hidden = false;
        $("lm-tasks-body").hidden = true;
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
  ["lm-f-status", "lm-f-submitter", "lm-f-text"].forEach((id) =>
    $(id).addEventListener("input", renderTasks));
  $("lm-f-clear").addEventListener("click", () => {
    $("lm-f-status").value = ""; $("lm-f-submitter").value = ""; $("lm-f-text").value = "";
    renderTasks();
  });
  $("lm-check-all").addEventListener("change", () => {
    const on = $("lm-check-all").checked;
    visibleTasks().forEach((t) => { if (on) selected.add(t.task_id); else selected.delete(t.task_id); });
    renderTasks();
  });
  $("lm-sel-all-tasks").addEventListener("click", () => {
    visibleTasks().forEach((t) => selected.add(t.task_id)); renderTasks();
  });
  $("lm-sel-none-tasks").addEventListener("click", () => { selected.clear(); renderTasks(); });
  $("lm-batch-download").addEventListener("click", batchDownload);
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
