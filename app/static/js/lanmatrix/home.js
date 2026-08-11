/* 工作台 (workspace home) — the cross-project landing page.
 *
 * Answers "what needs my attention" before the user has picked a project.
 * All filtering happens on the server (see routes/lanmatrix/me.py) so the
 * truncation notice means what it says: there really are more matching rows,
 * not just more rows that the client happened to hide.
 *
 * Filter state lives in the URL via LMUrl (see urlstate.js), matching the
 * convention established for the project task list, so a filtered workspace
 * can be linked, bookmarked and reached with the Back button.
 *
 * Views, not a stack
 * ------------------
 * 最近任务 / 待我审核 / 我的项目 are three different jobs, and stacking them on
 * one page meant the third was only reachable after scrolling past up to 200
 * tasks and every pending review. They are now tabs over panes -- the same
 * strip the project 设置 pages use -- and each list is paged client-side
 * (pager.js) instead of dumping every fetched row at once. `?view=` carries the
 * active tab, which is also what notification links already send (?view=reviews).
 */
(function (global) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TASK_LIMIT = 200;
  const TASK_PAGE_SIZE = 20;
  const PROJECT_PAGE_SIZE = 9;

  const VIEWS = ["tasks", "reviews", "projects"];
  const DEFAULT_VIEW = "tasks";
  let view = DEFAULT_VIEW;

  // Rows currently held for each paged list, so switching page repaints from
  // memory instead of re-querying the server.
  let taskRows = [];
  let taskProjects = {};
  let projectRows = [];

  let taskPager = null;
  let projectPager = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // --- status vocabulary --------------------------------------------------
  // Not re-implemented here: rows, badges, sort comparison and the action
  // buttons all come from LMTaskRow (task_row.js), the same module the project
  // task list renders with. A task that reads "运行中" over there cannot read
  // "running" here, and a button that appears there appears here too.
  const TR = global.LMTaskRow;
  const STATUS_ZH = LMPill.TASK_ZH;
  const pill = LMPill.html;
  const FINAL = TR.FINAL;

  function toast(msg, ok) {
    if (global.LMUI && LMUI.toast) LMUI.toast(msg, ok);
  }

  // --- views ---------------------------------------------------------------
  function normaliseView(v) {
    return VIEWS.indexOf(String(v || "")) >= 0 ? String(v) : DEFAULT_VIEW;
  }

  function paintView() {
    document.querySelectorAll("#lm-home-tabs .tab").forEach((b) => {
      const on = b.dataset.view === view;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    VIEWS.forEach((v) => {
      const pane = $(`lm-pane-${v}`);
      if (!pane) return;
      pane.classList.toggle("on", v === view);
      // hidden as well as the class: a display:none pane must also be out of
      // the accessibility tree, not just invisible.
      pane.hidden = v !== view;
    });
  }

  // push: a tab click is a navigation the user should be able to undo with
  // Back. Restoring from the URL is not, so it replaces instead.
  function setView(next, push) {
    const want = normaliseView(next);
    const changed = want !== view;
    view = want;
    paintView();
    // Polling only makes sense while the task pane is on screen.
    if (typeof ensurePolling === "function") ensurePolling();
    if (global.LMUrl) {
      const patch = { view: view === DEFAULT_VIEW ? null : view };
      if (push && changed) LMUrl.set(patch); else LMUrl.replace(patch);
    }
    return changed;
  }

  function setTabCount(key, n) {
    const el = document.querySelector(`#lm-home-tabs [data-tabcount="${key}"]`);
    if (el) el.textContent = n ? String(n) : "";
  }

  // --- filter state <-> URL -----------------------------------------------
  function getFilters() {
    return {
      status: $("lm-h-status").value || "",
      project: $("lm-h-project").value || "",
      mine: $("lm-h-mine").checked,
      q: $("lm-h-text").value.trim(),
    };
  }

  // replaceState, not pushState: a filter refines the current view. Pushing
  // would add one history entry per keystroke and Back would walk the user
  // letter-by-letter back out of their own search.
  function syncUrl() {
    if (!window.LMUrl) return;
    const f = getFilters();
    LMUrl.replace({ status: f.status, project: f.project,
      mine: f.mine ? "1" : null, q: f.q });
  }

  // The project <select> is populated from the API, so a ?project= value may
  // arrive before its <option> exists. Park it and apply once the list lands.
  let pendingProject = null;

  function hasOption(value) {
    const sel = $("lm-h-project");
    return Array.prototype.some.call(sel.options, (o) => o.value === String(value));
  }

  function applyUrlFilters() {
    if (!window.LMUrl) return;
    const all = LMUrl.all();
    $("lm-h-status").value = all.status || "";
    $("lm-h-mine").checked = all.mine === "1";
    $("lm-h-text").value = all.q || "";
    // On first load the <option>s do not exist yet, so park the value; on a
    // popstate they do, and applying it here is what makes Back restore the
    // project filter (the select is not repopulated on that path).
    const wantProject = all.project || null;
    if (wantProject && hasOption(wantProject)) {
      $("lm-h-project").value = String(wantProject);
      pendingProject = null;
    } else if (wantProject) {
      pendingProject = wantProject;
    } else {
      $("lm-h-project").value = "";
      pendingProject = null;
    }
    document.querySelectorAll("#lm-h-seg button").forEach((b) => {
      b.classList.toggle("on", (b.dataset.status || "") === (all.status || ""));
    });
    syncKpiSelection();
  }

  // The KPI tiles are filter shortcuts; keep their selected state in step with
  // however the filter was actually set (tile, segmented control, or URL).
  function syncKpiSelection() {
    const f = getFilters();
    document.querySelectorAll("#lm-kpi .kpi-tile[data-filter]").forEach((tile) => {
      const want = tile.dataset.filter;
      const on = want === "mine" ? (f.mine && !f.status) : (!f.mine && f.status === want);
      tile.classList.toggle("on", on);
      tile.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  // --- rendering ----------------------------------------------------------
  function renderKpi(kpi) {
    Object.keys(kpi || {}).forEach((k) => {
      const el = document.querySelector(`#lm-kpi [data-v="${k}"]`);
      if (el) el.textContent = kpi[k];
    });
  }

  function renderProjects(projects) {
    // Most-recently-touched first. Nothing is dropped any more: the list is
    // paged, so the 9th project is one click away instead of invisible.
    projectRows = (projects || []).slice()
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    setTabCount("projects", projectRows.length);
    if (projectPager) {
      projectPager.reset();
      projectPager.setTotal(projectRows.length);
    }
    paintProjects();
  }

  function paintProjects() {
    const host = $("lm-h-projects");
    if (!host) return;
    const rows = projectPager ? projectPager.slice(projectRows) : projectRows;
    if (!rows.length) { host.innerHTML = ""; return; }
    host.innerHTML = rows.map((p) => `
      <a class="proj-mini" href="/lanmatrix/projects/${encodeURIComponent(p.id)}/tasks">
        <div class="pm-top">
          <span class="pm-code">${esc(p.code || "")}</span>
          ${pill(p.status, STATUS_ZH[p.status] || p.status || "")}
        </div>
        <h3>${esc(p.name || p.code || "未命名项目")}</h3>
        <div class="pm-nums">
          <span>共 <b>${p.task_total || 0}</b></span>
          <span class="run">运行 <b>${p.task_running || 0}</b></span>
          <span class="ok">通过 <b>${p.task_passed || 0}</b></span>
          <span class="bad">失败 <b>${p.task_failed || 0}</b></span>
        </div>
      </a>`).join("");
  }

  function fillProjectSelect(projects) {
    const sel = $("lm-h-project");
    const keep = sel.value;
    const opts = (projects || []).slice()
      .sort((a, b) => String(a.code || "").localeCompare(String(b.code || "")));
    sel.innerHTML = '<option value="">全部项目</option>'
      + opts.map((p) => `<option value="${esc(p.id)}">${esc(p.code || p.name || p.id)}</option>`).join("");
    const want = pendingProject != null ? pendingProject : keep;
    if (want && hasOption(want)) sel.value = String(want);
    pendingProject = null;
  }

  // --- sorting -------------------------------------------------------------
  // Server-side ordering is newest-first; sorting is client-side over the rows
  // already fetched, exactly as on the project task list.
  // ``task_id`` is the default even though it no longer has a column: it is the
  // submission order, which is what "newest first" means, and it is the only key
  // guaranteed to be unique. With no matching header it simply shows no sort
  // arrow, which is exactly right -- that is the unsorted, natural order.
  let sortKey = "task_id";
  let sortDir = -1;

  function sortedTasks() {
    const rows = taskRows.slice();
    rows.sort((a, b) => {
      if (sortKey === "project") {
        const pa = taskProjects[a.project_id], pb = taskProjects[b.project_id];
        const x = String((pa && (pa.code || pa.name)) || a.project_id || "").toLowerCase();
        const y = String((pb && (pb.code || pb.name)) || b.project_id || "").toLowerCase();
        return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
      }
      return TR.cmp(a, b, sortKey) * sortDir;
    });
    return rows;
  }

  function updateSortIndicators() {
    document.querySelectorAll("#lm-pane-tasks thead th[data-sort]").forEach((th) => {
      const key = th.getAttribute("data-sort");
      const base = th.textContent.replace(/[ \u25b2\u25bc]+$/, "");
      th.textContent = key === sortKey ? base + (sortDir === 1 ? " \u25b2" : " \u25bc") : base;
      th.classList.toggle("lm-sorted", key === sortKey);
    });
  }

  function setSort(key) {
    if (!key) return;
    if (sortKey === key) sortDir = -sortDir;
    // Descending first for the keys whose interesting end is the high one:
    // newest run, latest finish, and 待审核 before 已通过.
    else {
      sortKey = key;
      sortDir = (key === "task_id" || key === "finished_at" || key === "review")
        ? -1 : 1;
    }
    updateSortIndicators();
    paintTasks();
  }

  // --- selection -----------------------------------------------------------
  // Kept across pages of the client-side pager and across polls: a selection
  // that silently empties when the table repaints is worse than no selection.
  const selected = new Set();

  function selectedTasks() {
    return taskRows.filter((t) => selected.has(t.task_id));
  }

  // The workspace spans projects, so every batch verb has to fan out per
  // project_id — all task endpoints are /projects/<pid>/tasks/...
  function groupByProject(tasks) {
    const out = new Map();
    tasks.forEach((t) => {
      const pid = t.project_id;
      if (!pid) return;
      if (!out.has(pid)) out.set(pid, []);
      out.get(pid).push(t.task_id);
    });
    return out;
  }

  function canDeleteIn(projectId) {
    const p = taskProjects[projectId];
    return !!(p && p.can_delete);
  }

  function updateBatchBar() {
    const n = selectedTasks().length;
    const st = $("lm-h-batch-status");
    if (st) st.textContent = n ? `已选 ${n} 个` : "";
    const all = $("lm-h-check-all");
    if (all) all.checked = taskRows.length > 0 && taskRows.every((t) => selected.has(t.task_id));
    // 删除所选 only appears when at least one selected row lives in a project
    // the user may delete in — task.delete is project_admin only, and the
    // workspace mixes projects with different roles.
    const del = $("lm-h-batch-delete");
    if (del) del.hidden = !selectedTasks().some((t) => canDeleteIn(t.project_id));
  }

  function renderTasks(data) {
    taskRows = (data && data.tasks) || [];
    // Server ships the project lookup with the payload — a cross-project list
    // showing bare numeric ids would defeat the point of the page. It also
    // carries per-project capabilities, because 删除 is allowed in some of the
    // listed projects and not in others.
    taskProjects = {};
    ((data && data.projects) || []).forEach((p) => { taskProjects[p.id] = p; });

    // Drop selections whose rows are gone (filter changed, task deleted).
    const present = new Set(taskRows.map((t) => t.task_id));
    Array.from(selected).forEach((k) => { if (!present.has(k)) selected.delete(k); });

    $("lm-home-count").textContent = taskRows.length ? `${taskRows.length} 条` : "";
    setTabCount("tasks", taskRows.length);
    $("lm-h-more").hidden = !(data && data.truncated);

    // A new result set always starts at page 1: staying on page 7 of the
    // previous filter would show an empty table and look like a failure.
    if (taskPager) {
      taskPager.reset();
      taskPager.setTotal(taskRows.length);
    }
    paintTasks();
    ensurePolling();
  }

  function projectCell(t) {
    const p = taskProjects[t.project_id];
    if (!p) return '<td><span class="muted">—</span></td>';
    return `<td><a class="proj-cell" href="/lanmatrix/projects/${encodeURIComponent(p.id)}/tasks"`
      + ` title="${esc(p.name || "")}">${esc(p.code || p.name || p.id)}</a></td>`;
  }

  function taskHref(t) {
    if (!t.project_id) return null;
    // ?from=workspace makes the project page offer 返回我的工作台 instead of a
    // dead end after the user drilled in from here.
    return `/lanmatrix/projects/${encodeURIComponent(t.project_id)}/tasks`
      + `?task=${encodeURIComponent(t.task_id || "")}&from=workspace`;
  }

  function rowHtml(t) {
    return TR.rowHtml(t, {
      projectId: t.project_id,
      canDelete: canDeleteIn(t.project_id),
      selected: selected.has(t.task_id),
      projectCell: projectCell(t),
      // The detail panel (live log + judge result) lives on the project page;
      // the workspace links into it rather than shipping a second copy.
      viewHref: taskHref(t),
    });
  }

  function paintTasks() {
    const body = $("lm-h-rows");
    const empty = $("lm-h-empty");
    if (!body) return;

    if (!taskRows.length) {
      body.innerHTML = "";
      empty.hidden = false;
      updateBatchBar();
      return;
    }
    empty.hidden = true;

    const rows = sortedTasks();
    const page = taskPager ? taskPager.slice(rows) : rows;
    body.innerHTML = page.map(rowHtml).join("");
    rowSig = {};
    page.forEach((t) => { rowSig[t.task_id] = TR.signature(t); });
    bindRowActions();
    updateBatchBar();
  }

  // --- row actions ---------------------------------------------------------
  // Every button carries data-p (project id) as well as data-k (task id),
  // because on this page two rows next to each other can belong to different
  // projects and every task endpoint is scoped by project.
  function bindRowActions() {
    const host = $("lm-h-rows");
    if (!host) return;
    host.querySelectorAll(".lm-task-cancel").forEach((b) =>
      b.addEventListener("click", () => cancelOne(b.dataset.p, b.dataset.k)));
    host.querySelectorAll(".lm-task-retest").forEach((b) =>
      b.addEventListener("click", () => retest([findTask(b.dataset.k)].filter(Boolean))));
    host.querySelectorAll(".lm-task-del").forEach((b) =>
      b.addEventListener("click", () => deleteOne(b.dataset.p, b.dataset.k)));
    host.querySelectorAll(".lm-task-steps").forEach((b) =>
      b.addEventListener("click", () => openSteps(b.dataset.p, b.dataset.tid)));
    host.querySelectorAll(".lm-task-sel").forEach((cb) =>
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(cb.dataset.k); else selected.delete(cb.dataset.k);
        updateBatchBar();
      }));
  }

  function findTask(key) {
    return taskRows.find((t) => t.task_id === key) || null;
  }

  async function cancelOne(projectId, key) {
    try {
      await LMApi.cancelProjectTask(projectId, key);
      toast("已请求取消", true);
      loadTasks();
    } catch (ex) { toast(ex.message, false); }
  }

  async function deleteOne(projectId, key) {
    if (!(await LMUI.confirm({
      level: "danger",
      title: "删除该任务",
      body: `任务 ${key} 及其工作区与报告将被删除，此操作不可撤销。`,
      confirmText: "删除",
    }))) return;
    try {
      await LMApi.deleteProjectTask(projectId, key);
      selected.delete(key);
      toast("任务已删除", true);
      loadTasks();
    } catch (ex) { toast(ex.message, false); }
  }

  // --- batch actions -------------------------------------------------------
  // Confirmed once, then fanned out per project. Results are summed and the
  // shortfall is reported: "已取消 8 个，2 个失败" beats a blanket "已取消".
  function batchDownload() {
    const eligible = selectedTasks().filter((t) => t.has_result);
    if (!eligible.length) { toast("所选任务均无可下载的报告", false); return; }
    const groups = groupByProject(eligible);
    if (groups.size > 1) {
      toast(`所选任务分属 ${groups.size} 个项目，将分别下载`, true);
    }
    // One navigation per project; opened in sequence so the browser does not
    // discard all but the last download.
    let i = 0;
    groups.forEach((keys, projectId) => {
      const url = LMApi.projectTasksDownloadBatchUrl(projectId, keys);
      if (i === 0) window.location = url;
      else setTimeout(() => { window.open(url, "_blank"); }, i * 400);
      i++;
    });
  }

  async function batchCancel() {
    const eligible = selectedTasks().filter((t) => !FINAL.includes(t.status));
    if (!eligible.length) { toast("所选任务没有可取消的运行", false); return; }
    if (!(await LMUI.confirm({
      level: "danger",
      title: `取消 ${eligible.length} 个任务`,
      body: "所选运行中/排队中的任务将立即停止，已产生的部分结果会保留。",
      confirmText: "取消任务",
      cancelText: "返回",
    }))) return;
    let ok = 0, bad = 0;
    for (const [projectId, keys] of groupByProject(eligible)) {
      for (const k of keys) {
        try { await LMApi.cancelProjectTask(projectId, k); ok++; }
        catch (_e) { bad++; }
      }
    }
    toast(bad ? `已请求取消 ${ok} 个，${bad} 个失败` : `已请求取消 ${ok} 个任务`, !bad);
    loadTasks();
  }

  async function batchDelete() {
    const eligible = selectedTasks().filter((t) => canDeleteIn(t.project_id));
    if (!eligible.length) { toast("所选任务中没有你有权限删除的任务", false); return; }
    const skipped = selectedTasks().length - eligible.length;
    if (!(await LMUI.confirm({
      level: "critical",
      title: `删除 ${eligible.length} 个任务`,
      body: "任务及其工作区与报告将一并删除，此操作不可撤销。"
        + (skipped ? `另有 ${skipped} 个任务因权限不足会被跳过。` : ""),
      requireText: String(eligible.length),
      confirmText: "永久删除",
    }))) return;
    let ok = 0, bad = 0;
    for (const [projectId, keys] of groupByProject(eligible)) {
      try {
        const data = await LMApi.deleteProjectTasksBatch(projectId, keys);
        ok += (data.results || []).filter((r) => r.result === "deleted").length;
        keys.forEach((k) => selected.delete(k));
      } catch (_e) { bad += keys.length; }
    }
    toast(bad ? `已删除 ${ok} 个，${bad} 个失败` : `${ok} 个任务已删除`, !bad);
    loadTasks();
  }

  async function retest(tasks) {
    const eligible = (tasks || []).filter((t) => TR.canRetest(t));
    if (!eligible.length) { toast("所选任务均已在队列中，无需重新测试", false); return; }
    if (!(await LMUI.confirm({
      level: "danger",
      title: `重测 ${eligible.length} 个任务`,
      body: "任务将重新加入测试队列，原有结果会被覆盖。",
      confirmText: "重新测试",
    }))) return;
    let created = 0, skipped = 0, missing = 0, errs = 0;
    for (const [projectId, keys] of groupByProject(eligible)) {
      try {
        const data = await LMApi.rerunSelectedTasks(projectId, keys);
        created += (data.created || []).length;
        skipped += (data.skipped || []).length;
        missing += (data.missing || []).length;
        errs += (data.errors || []).length;
      } catch (_e) { errs += keys.length; }
    }
    let msg = `已重新加入队列 ${created} 个任务`;
    const extra = [];
    if (skipped) extra.push(`跳过 ${skipped} 个（已在队列）`);
    if (missing) extra.push(`${missing} 个 test id 已失效`);
    if (errs) extra.push(`${errs} 个失败`);
    if (extra.length) msg += `，${extra.join("，")}`;
    toast(msg, created > 0);
    loadTasks();
  }

  function batchRetest() { return retest(selectedTasks()); }

  // --- 手顺 dialog ----------------------------------------------------------
  // Same editor as the project task list. Test items are fetched per project
  // and cached, because the workspace can open steps for any of them.
  const itemsCache = {};
  async function fetchTestItems(projectId) {
    if (itemsCache[projectId]) return itemsCache[projectId];
    const acc = [];
    let p = 1;
    for (;;) {
      const data = await LMApi.listItems(projectId, { page: p, page_size: 500, sheet: "test" });
      const batch = (data && data.items) || [];
      acc.push.apply(acc, batch);
      if (acc.length >= ((data && data.total) || 0) || batch.length === 0) break;
      p++;
      if (p > 2000) break;
    }
    itemsCache[projectId] = acc;
    return acc;
  }

  async function fetchSheetItems(projectId, sheet) {
    const acc = [];
    let p = 1;
    for (;;) {
      const data = await LMApi.listItems(projectId, { page: p, page_size: 500, sheet });
      const batch = (data && data.items) || [];
      acc.push.apply(acc, batch);
      if (acc.length >= ((data && data.total) || 0) || batch.length === 0) break;
      p++;
      if (p > 2000) break;
    }
    return acc;
  }

  async function openSteps(projectId, testId) {
    if (!global.LMStepsEditor) { toast("步骤编辑器未加载", false); return; }
    const tid = String(testId == null ? "" : testId).trim();
    if (!tid) { toast("该任务缺少 test id", false); return; }
    try {
      const items = await fetchTestItems(projectId);
      const row = items.find((it) => String(it.test_id == null ? "" : it.test_id).trim() === tid);
      if (!row) { toast(`在测试表中未找到 test id「${tid}」对应的行`, false); return; }
      LMStepsEditor.open(row, {
        fieldKey: "steps",
        testId: tid,
        onSave: async (json) => {
          const data = await LMApi.patchItem(projectId, row.id, row.version, { steps: json });
          row.version = data.item.version;
          row.steps = data.item.steps;
          toast("步骤明细已保存", true);
        },
        loadRef: async () => {
          const grab = async (sheet) => {
            try { return await fetchSheetItems(projectId, sheet); } catch (_e) { return []; }
          };
          const [lib, cst, io] = await Promise.all([grab("lib"), grab("const"), grab("io")]);
          return { lib, const: cst, io };
        },
      });
    } catch (ex) { toast(ex.message, false); }
  }

  // --- live progress -------------------------------------------------------
  // One periodic poll of the whole list (not one stream per row): the browser
  // caps concurrent connections per origin, and a workspace full of running
  // rows would starve every other request on the page. Rows whose state did not
  // change are left alone, so scrolling and checkboxes survive the refresh.
  const POLL_MS = 4000;
  let pollTimer = null;
  let rowSig = {};

  function anyLive() {
    return taskRows.some((t) => !FINAL.includes(t.status));
  }
  function ensurePolling() {
    if (anyLive() && view === "tasks") startPolling(); else stopPolling();
  }
  function startPolling() {
    if (!pollTimer) pollTimer = setInterval(pollTasks, POLL_MS);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  async function pollTasks() {
    if (document.hidden || view !== "tasks") return;
    let data;
    try { data = await LMApi.meTasks(taskParams()); }
    catch (_e) { return; }   // transient: keep the last snapshot, retry next tick
    const tasks = data.tasks || [];
    const oldIds = taskRows.map((t) => t.task_id).join(",");
    const newIds = tasks.map((t) => t.task_id).join(",");
    if (oldIds !== newIds) { renderTasks(data); return; }  // set changed → full repaint
    taskRows = tasks;
    let patched = false;
    tasks.forEach((t) => {
      const sig = TR.signature(t);
      if (rowSig[t.task_id] === sig) return;
      rowSig[t.task_id] = sig;
      const row = document.querySelector(`#lm-h-rows tr[data-k="${cssEscape(t.task_id)}"]`);
      if (row) { row.outerHTML = rowHtml(t); patched = true; }
    });
    if (patched) { bindRowActions(); updateBatchBar(); }
    ensurePolling();
  }

  function cssEscape(s) { return String(s).replace(/"/g, '\\"'); }

  // --- loading ------------------------------------------------------------
  function showError(msg) {
    $("lm-home-err-msg").textContent = msg || "请稍后重试。";
    $("lm-home-err").hidden = false;
    $("lm-home-body").hidden = true;
    $("lm-home-empty").hidden = true;
  }

  async function loadOverview() {
    const data = await LMApi.meOverview();
    renderKpi(data.kpi);
    const projects = data.projects || [];
    fillProjectSelect(projects);
    renderProjects(projects);
    $("lm-home-err").hidden = true;
    if (!projects.length) {
      $("lm-home-empty").hidden = false;
      $("lm-home-body").hidden = true;
      return false;
    }
    $("lm-home-empty").hidden = true;
    $("lm-home-body").hidden = false;
    return true;
  }

  // Guard against out-of-order responses: a fast empty query must not overwrite
  // the results of a slower one the user typed first.
  // Shared by the initial load and the live poll, so a poll can never widen or
  // narrow the result set the user is actually looking at.
  function taskParams() {
    const f = getFilters();
    const params = {};
    if (f.status) params.status = f.status;
    if (f.project) params.project_id = f.project;
    if (f.mine) params.mine = "1";
    if (f.q) params.q = f.q;
    params.limit = TASK_LIMIT;
    return params;
  }

  let seq = 0;
  async function loadTasks() {
    const my = ++seq;
    try {
      const data = await LMApi.meTasks(taskParams());
      if (my !== seq) return;
      renderTasks(data);
    } catch (ex) {
      if (my !== seq) return;
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      $("lm-h-rows").innerHTML =
        `<tr><td colspan="10" class="muted">加载失败：${esc(ex.message || "")}</td></tr>`;
      $("lm-h-empty").hidden = true;
      $("lm-home-count").textContent = "";
    }
  }

  async function loadAll() {
    try {
      const hasProjects = await loadOverview();
      if (hasProjects) await loadTasks();
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      showError(ex.message);
    }
  }

  // --- wiring -------------------------------------------------------------
  function onFilterChange() {
    syncUrl();
    syncKpiSelection();
    loadTasks();
  }

  let typing = null;
  function onTyping() {
    clearTimeout(typing);
    typing = setTimeout(onFilterChange, 250);
  }

  function setStatus(status) {
    $("lm-h-status").value = status || "";
    document.querySelectorAll("#lm-h-seg button").forEach((b) => {
      b.classList.toggle("on", (b.dataset.status || "") === (status || ""));
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (global.LMPager) {
      taskPager = LMPager.create({
        host: $("lm-h-pager"), pageSize: TASK_PAGE_SIZE, onChange: paintTasks,
      });
      projectPager = LMPager.create({
        host: $("lm-h-proj-pager"), pageSize: PROJECT_PAGE_SIZE,
        onChange: paintProjects,
      });
    }

    const tabs = $("lm-home-tabs");
    if (tabs) tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (btn) setView(btn.dataset.view, true);
    });

    // Sortable headers, mirroring the project task list.
    document.querySelectorAll("#lm-pane-tasks thead th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => setSort(th.getAttribute("data-sort")));
    });
    updateSortIndicators();

    // 全选 covers every row matching the current filter, not just the visible
    // page: a "select all" that stops at the page boundary is a trap when the
    // list is paged 20 at a time.
    const checkAll = $("lm-h-check-all");
    if (checkAll) checkAll.addEventListener("change", () => {
      if (checkAll.checked) taskRows.forEach((t) => selected.add(t.task_id));
      else selected.clear();
      paintTasks();
    });

    const batch = {
      "lm-h-batch-download": batchDownload,
      "lm-h-batch-cancel": batchCancel,
      "lm-h-batch-retest": batchRetest,
      "lm-h-batch-delete": batchDelete,
    };
    Object.keys(batch).forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("click", batch[id]);
    });

    $("lm-h-text").addEventListener("input", onTyping);
    $("lm-h-project").addEventListener("change", onFilterChange);
    $("lm-h-mine").addEventListener("change", onFilterChange);

    document.querySelectorAll("#lm-h-seg button").forEach((b) => {
      b.addEventListener("click", () => {
        setStatus(b.dataset.status || "");
        onFilterChange();
      });
    });

    document.querySelectorAll("#lm-kpi .kpi-tile[data-filter]").forEach((tile) => {
      tile.addEventListener("click", () => {
        const want = tile.dataset.filter;
        const on = tile.classList.contains("on");
        // Clicking the active tile clears it — a filter you can turn on but not
        // off is a trap, and the tile is the only affordance in reach.
        if (want === "mine") {
          $("lm-h-mine").checked = !on;
          if (!on) setStatus("");
        } else {
          $("lm-h-mine").checked = false;
          setStatus(on ? "" : want);
        }
        // The tiles filter the task list, so they must also bring it into
        // view — otherwise clicking 失败 from the review tab changes a list the
        // user cannot see and looks like nothing happened.
        setView("tasks", true);
        onFilterChange();
      });
    });

    $("lm-home-refresh").addEventListener("click", loadAll);
    $("lm-home-retry").addEventListener("click", loadAll);

    // Back/forward restores the whole filtered view, not just one control.
    if (global.LMUrl) {
      LMUrl.onPop((all) => {
        setView(all.view);
        applyUrlFilters();
        loadTasks();
      });
    }

    setView(global.LMUrl ? LMUrl.get("view", DEFAULT_VIEW) : DEFAULT_VIEW);
    applyUrlFilters();
    loadAll();
  });

  // Exposed so the review queue (workspace_reviews.js) can raise its own tab
  // and publish its count without either module reaching into the other's DOM.
  global.LMHome = { setView, setTabCount, view: () => view };
})(window);
