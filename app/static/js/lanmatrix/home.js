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
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TASK_LIMIT = 200;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // --- status vocabulary --------------------------------------------------
  // Deliberately identical to project_tasks.js: a task that reads "运行中" on
  // the project page must not read "running" here.
  const STATUS_ZH = LMPill.TASK_ZH;

  const pill = LMPill.html;
  // A finished-but-failing run carries status "failed"; split a genuine test
  // failure (verdict FAIL) from an execution/judge error (verdict ERROR).
  function mergedBadge(t) {
    const st = String(t.status || "").toLowerCase();
    let cls = st || "notask";
    if (st === "failed" && String(t.result || "").trim().toUpperCase().startsWith("ERROR")) {
      cls = "error";
    }
    return pill(cls, STATUS_ZH[cls] || cls, String(t.result || t.status || ""));
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getFullYear() % 100)}/${p(d.getMonth() + 1)}/${p(d.getDate())} `
      + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
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
    const host = $("lm-h-projects");
    // Most-recently-touched first, capped: this strip is a shortcut, and the
    // full management surface is one click away on the projects page.
    const rows = (projects || []).slice()
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))
      .slice(0, 8);
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

  function renderTasks(data) {
    const tasks = (data && data.tasks) || [];
    // Server ships the project lookup with the payload — a cross-project list
    // showing bare numeric ids would defeat the point of the page.
    const names = {};
    ((data && data.projects) || []).forEach((p) => { names[p.id] = p; });

    const body = $("lm-h-rows");
    const empty = $("lm-h-empty");
    const more = $("lm-h-more");

    $("lm-home-count").textContent = tasks.length ? `${tasks.length} 条` : "";
    more.hidden = !(data && data.truncated);

    if (!tasks.length) {
      body.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    body.innerHTML = tasks.map((t) => {
      const p = names[t.project_id];
      // Deep-link straight into the task's detail panel on the project page,
      // using the ?task= contract the task list already understands.
      const href = t.project_id
        ? `/lanmatrix/projects/${encodeURIComponent(t.project_id)}/tasks?task=${encodeURIComponent(t.task_id || "")}`
        : null;
      const proj = p
        ? `<span class="proj-cell" title="${esc(p.name || "")}">${esc(p.code || p.name || p.id)}</span>`
        : '<span class="muted">—</span>';
      return `<tr>
        <td>${proj}</td>
        <td>${href ? `<a href="${href}">${esc(t.task_id || "")}</a>` : esc(t.task_id || "")}</td>
        <td>${esc(t.test_id || "")}</td>
        <td>${esc(t.submitter || "")}</td>
        <td>${mergedBadge(t)}</td>
        <td class="muted">${esc(fmtTime(t.finished_at))}</td>
        <td>${href ? `<a class="btn ghost small" href="${href}">查看</a>` : ""}</td>
      </tr>`;
    }).join("");
  }

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
  let seq = 0;
  async function loadTasks() {
    const my = ++seq;
    const f = getFilters();
    const params = {};
    if (f.status) params.status = f.status;
    if (f.project) params.project_id = f.project;
    if (f.mine) params.mine = "1";
    if (f.q) params.q = f.q;
    params.limit = TASK_LIMIT;
    try {
      const data = await LMApi.meTasks(params);
      if (my !== seq) return;
      renderTasks(data);
    } catch (ex) {
      if (my !== seq) return;
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      $("lm-h-rows").innerHTML =
        `<tr><td colspan="7" class="muted">加载失败：${esc(ex.message || "")}</td></tr>`;
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
        onFilterChange();
      });
    });

    $("lm-home-refresh").addEventListener("click", loadAll);
    $("lm-home-retry").addEventListener("click", loadAll);

    // Back/forward restores the whole filtered view, not just one control.
    if (window.LMUrl) {
      LMUrl.onPop(() => {
        applyUrlFilters();
        loadTasks();
      });
    }

    applyUrlFilters();
    loadAll();
  });
})();
