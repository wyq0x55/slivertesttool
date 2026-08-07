/* System-admin console: account management (submitters + admins), .sil model
 * registry, license concurrency, and cross-project task management. Authority
 * comes from the logged-in System Administrator account — every endpoint under
 * /api/v1/admin/* is gated to is_system_admin, so no ADMIN_TOKEN is needed. */
(function () {
  "use strict";
  const root = document.querySelector(".lm-admin");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const FINAL = ["passed", "failed", "cancelled"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  // Deterministic gradient avatar (2-char label) — mirrors the design demo.
  function avatar(label, i) {
    const h1 = (i * 67) % 360, h2 = (i * 67 + 40) % 360;
    return `<span class="avatar" style="width:30px;height:30px;font-size:11px;`
      + `background:linear-gradient(135deg,hsl(${h1} 55% 62%),hsl(${h2} 55% 50%))">`
      + `${esc(label)}</span>`;
  }
  const ICO_EDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  const ICO_DEL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>';

  // Merge execution ``status`` + judge ``result`` into one label, splitting a
  // failed run into a genuine ``failed`` (verdict FAIL) vs an ``error`` (ERROR).
  const STATUS_ZH = { queued: "排队中", running: "运行中", passed: "通过",
    failed: "失败", error: "异常", cancelled: "已取消", notask: "—" };
  function pill(cls, label, tip) {
    const c = cls || "notask";
    return `<span class="pill st-${esc(c)}" title="${esc(tip || label)}"><span class="dot"></span>${esc(label)}</span>`;
  }
  function mergedBadge(t) {
    const st = String(t.status || "").toLowerCase();
    let cls = st || "notask";
    let label = st || "—";
    if (st === "failed") {
      const v = String(t.result || "").trim().toUpperCase();
      if (v.startsWith("ERROR")) { cls = "error"; label = "error"; }
    }
    return pill(cls, STATUS_ZH[label] || label, t.result || t.status || "");
  }
  function progCell(p) {
    p = p || 0;
    return `<div style="display:flex;align-items:center;gap:8px">
      <div class="prog" style="width:70px;flex:0 0 auto"><i style="width:${p}%"></i></div>
      <span class="muted" style="font-size:12px">${p}%</span></div>`;
  }
  // Completion moment as ``YY/MM/DD HH:MM:SS`` local time, e.g. 26/07/20 11:18:15.
  function fmtFinished(t) {
    if (!t.finished_at) return "";
    const d = new Date(t.finished_at);
    if (isNaN(d.getTime())) return "";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getFullYear() % 100)}/${p(d.getMonth() + 1)}/${p(d.getDate())} `
      + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  function toast(msg, ok) {
    const t = $("lm-toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3200);
  }

  // --- tabs ---------------------------------------------------------------- //
  // The active tab lives in `?tab=`, so an admin can link straight to 授权 or
  // 任务 and Back steps between tabs instead of leaving the page.
  const loaders = { users: loadUsers, license: loadLicense, tasks: loadTasks };
  const TABS = Object.keys(loaders);
  const DEFAULT_TAB = TABS[0];

  // Single source of truth for "show tab X": both the click handler and the
  // URL restore path go through here, so they can never drift apart.
  function activateTab(name, opts) {
    opts = opts || {};
    if (!TABS.includes(name)) name = DEFAULT_TAB;
    root.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("on", t.dataset.tab === name));
    root.querySelectorAll(".pane").forEach((p) => {
      const on = p.dataset.pane === name;
      p.classList.toggle("on", on);
      p.hidden = !on;
    });
    if (opts.load !== false) (loaders[name] || (() => {}))();
    if (opts && opts.push && window.LMUrl) {
      // Default tab writes no param, keeping /admin clean rather than
      // /admin?tab=users for the view you get by just clicking the nav link.
      LMUrl.set({ tab: name === DEFAULT_TAB ? null : name });
    }
  }

  root.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab, { push: true }));
  });

  if (window.LMUrl) {
    LMUrl.onPop((q) => activateTab(q.tab || DEFAULT_TAB));
  }

  // --- accounts ------------------------------------------------------------ //
  async function loadUsers() {
    const tb = $("lm-user-rows");
    try {
      const data = await LMApi.adminListUsers();
      const users = data.users || [];
      const kUsers = $("lm-kpi-users"); if (kUsers) kUsers.textContent = users.length;
      const admins = users.filter((u) => u.is_system_admin).length;
      const kut = $("lm-kpi-users-trend");
      if (kut) kut.textContent = admins ? admins + " 管理员" : "—";
      tb.innerHTML = users.map((u, i) => {
        const active = u.status === "active";
        const name = u.display_name || u.username || "?";
        const projects = Array.isArray(u.projects) ? u.projects : [];
        const chips = projects.length
          ? `<div class="projchips">${projects.map((p) => `<span class="pchip">${esc(p)}</span>`).join("")}</div>`
          : '<span class="muted">—</span>';
        return `
        <tr>
          <td><div class="u-name">${avatar(name.slice(0, 2), i)}<div><b>${esc(u.username)}</b><span class="sub">${esc(u.display_name || "")}</span></div></div></td>
          <td class="mono">${esc(u.email || "")}</td>
          <td>${pill(active ? "on" : "off", active ? "启用" : "停用")}</td>
          <td><label class="sw"><input type="checkbox" class="lm-user-admin-sw" data-id="${u.id}" ${u.is_system_admin ? "checked" : ""}><span class="track"></span></label></td>
          <td>${chips}</td>
          <td><div class="row-acts">
            <button class="btn small btn-icon lm-user-edit" data-id="${u.id}" title="编辑">${ICO_EDIT}</button>
            <button class="btn small btn-icon danger lm-user-del" data-id="${u.id}" title="删除">${ICO_DEL}</button>
          </div></td>
        </tr>`;
      }).join("") || '<tr><td colspan="6" class="muted">暂无账号</td></tr>';
      window._lmUsers = users;
      tb.querySelectorAll(".lm-user-edit").forEach((b) =>
        b.addEventListener("click", () => openUser(Number(b.dataset.id))));
      tb.querySelectorAll(".lm-user-del").forEach((b) =>
        b.addEventListener("click", () => delUser(Number(b.dataset.id))));
      tb.querySelectorAll(".lm-user-admin-sw").forEach((sw) =>
        sw.addEventListener("change", () => toggleAdmin(Number(sw.dataset.id), sw.checked, sw)));
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      tb.innerHTML = `<tr><td colspan="6" class="lm-err">${esc(ex.message)}</td></tr>`;
    }
  }

  // Flip is_system_admin straight from the row switch; revert on failure.
  async function toggleAdmin(id, value, sw) {
    try {
      await LMApi.adminUpdateUser(id, { is_system_admin: value });
      const u = (window._lmUsers || []).find((x) => x.id === id);
      if (u) u.is_system_admin = value;
      const admins = (window._lmUsers || []).filter((x) => x.is_system_admin).length;
      const kut = $("lm-kpi-users-trend");
      if (kut) kut.textContent = admins ? admins + " 管理员" : "—";
      toast(value ? "已设为系统管理员" : "已取消系统管理员", true);
    } catch (ex) {
      if (sw) sw.checked = !value;
      toast(ex.message || "更新失败", false);
    }
  }

  const dlg = $("lm-user-dialog");
  function openUser(id) {
    const u = (window._lmUsers || []).find((x) => x.id === id) || null;
    $("lm-user-error").hidden = true;
    $("lm-user-dlg-title").textContent = u ? "编辑账号" : "新建账号";
    $("lm-user-id").value = u ? u.id : "";
    $("lm-user-username").value = u ? u.username : "";
    $("lm-user-username").disabled = !!u;
    $("lm-user-display").value = u ? (u.display_name || "") : "";
    $("lm-user-email").value = u ? (u.email || "") : "";
    $("lm-user-password").value = "";
    $("lm-user-admin").checked = u ? !!u.is_system_admin : false;
    $("lm-user-active").checked = u ? u.status === "active" : true;
    dlg.showModal();
  }
  $("lm-user-new").addEventListener("click", () => openUser(null));
  $("lm-user-ok").addEventListener("click", async (e) => {
    e.preventDefault();
    const id = $("lm-user-id").value;
    const err = $("lm-user-error");
    err.hidden = true;
    try {
      if (id) {
        const changes = {
          display_name: $("lm-user-display").value,
          email: $("lm-user-email").value,
          is_system_admin: $("lm-user-admin").checked,
          status: $("lm-user-active").checked ? "active" : "disabled",
        };
        if ($("lm-user-password").value) changes.password = $("lm-user-password").value;
        await LMApi.adminUpdateUser(Number(id), changes);
      } else {
        await LMApi.adminCreateUser({
          username: $("lm-user-username").value,
          display_name: $("lm-user-display").value,
          email: $("lm-user-email").value,
          password: $("lm-user-password").value,
          is_system_admin: $("lm-user-admin").checked,
          status: $("lm-user-active").checked ? "active" : "disabled",
        });
      }
      dlg.close();
      toast("账号已保存", true);
      loadUsers();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });
  async function delUser(id) {
    if (!(await LMUI.confirm({
      level: "critical",
      title: "删除该账号",
      body: "该操作会同时移除此账号在所有项目中的成员身份，且不可撤销。",
      requireText: "DELETE",
      confirmText: "永久删除账号",
    }))) return;
    try { await LMApi.adminDeleteUser(id); toast("账号已删除", true); loadUsers(); }
    catch (ex) { toast(ex.message, false); }
  }

  // Model registration lives on the per-project 模型管理 page; it was removed
  // from the system console to avoid a duplicate, global surface.

  // --- license ------------------------------------------------------------- //
  function paintLicense(l) {
    l = l || {};
    const total = l.total || 0, inUse = l.in_use || 0;
    const pct = total > 0 ? Math.round((inUse / total) * 100) : 0;
    const ring = $("lm-ring"); if (ring) ring.style.setProperty("--p", pct);
    const rp = $("lm-ring-pct"); if (rp) rp.textContent = pct + "%";
    const info = $("lm-license-info");
    if (info) info.textContent =
      `总量 ${total} · 使用中 ${inUse} · 空闲 ${l.available || 0} · 排队 ${l.queued_jobs || 0}`;
    const ki = $("lm-kpi-inuse"); if (ki) ki.textContent = inUse;
    const klt = $("lm-kpi-lic-trend");
    if (klt) klt.textContent = total ? `${inUse}/${total}` : "—";
    _licServer = total || 1;
    const cnt = $("lm-license-count"); if (cnt) cnt.value = _licServer;
  }
  // Last concurrency limit fetched from the server, used to compute the dirty set
  // (the 并发上限 control now shares the runtime-config card's single 保存 button).
  let _licServer = 1;

  // Headline stats sourced independently of the tabs: running-task count and
  // project totals. Failures degrade gracefully to a dash.
  async function loadStats() {
    try {
      const data = await LMApi.adminListTasks();
      const tasks = data.tasks || [];
      const running = tasks.filter((t) =>
        String(t.status || "").toLowerCase() === "running").length;
      const kr = $("lm-kpi-running"); if (kr) kr.textContent = running;
      const krt = $("lm-kpi-run-trend");
      if (krt) {
        krt.textContent = running ? "运行中" : "空闲";
        krt.className = "trend " + (running ? "up" : "flat");
      }
    } catch (_e) { /* keep dash */ }
    try {
      const data = await LMApi.listProjects();
      const projects = data.projects || [];
      const active = projects.filter((p) => (p.status || "") === "active").length;
      const kp = $("lm-kpi-projects"); if (kp) kp.textContent = projects.length;
      const kpt = $("lm-kpi-proj-trend");
      if (kpt) kpt.textContent = projects.length ? `活跃 ${active}` : "—";
    } catch (_e) { /* keep dash */ }
  }
  async function loadLicense() {
    try {
      const data = await LMApi.adminGetLicense();
      paintLicense(data.license || {});
    } catch (ex) { const info = $("lm-license-info"); if (info) info.textContent = ex.message; }
    loadRuntimeConfig();
  }
  function stepLicense(d) {
    const el = $("lm-license-count");
    el.value = Math.max(1, (Number(el.value) || 1) + d);
  }
  const licDec = $("lm-lic-dec"); if (licDec) licDec.addEventListener("click", () => stepLicense(-1));
  const licInc = $("lm-lic-inc"); if (licInc) licInc.addEventListener("click", () => stepLicense(1));

  // --- runtime config (hot-reloadable) ------------------------------------ //
  // UI labels/help live here (the backend registry stays language-neutral and
  // only carries type/min/max/default metadata).
  const RTC_META = {
    silver_pool_enabled: {
      label: "Silver 实例池",
      help: "启用预热实例池复用；关闭则每个任务单独启动 Silver 实例",
    },
    silver_pool_prewarm: {
      label: "启动即预热",
      help: "Worker 启动时立即预热并占用许可，否则首次需要时才创建",
    },
    silver_pool_reconcile_seconds: {
      label: "实例池同步间隔",
      help: "Worker 将池大小与并发上限对齐的周期",
      unit: "秒", step: 0.5,
    },
    execution_timeout: {
      label: "执行超时",
      help: "单个测试的最长运行时间，超时判定为失败",
      unit: "秒", step: 1,
    },
    silver_gui: {
      label: "Silver 图形界面",
      help: "以 GUI 方式启动 Silver；仅对新启动 / 专用实例生效",
    },
    task_event_retention: {
      label: "任务事件保留条数",
      help: "每个任务最多保留的历史事件（日志/进度）条数；每日凌晨自动清理，也可在任务管理中手动清理",
      unit: "条", step: 100,
    },
  };
  // The last values fetched from the server, used to compute the dirty set.
  let _rtcServer = {};

  function rtcRow(field) {
    const meta = RTC_META[field.key] || { label: field.key, help: "" };
    const unit = meta.unit ? ` <span class="muted" style="font-size:12px">${esc(meta.unit)}</span>` : "";
    let control;
    if (field.type === "bool") {
      control = `<label class="sw"><input type="checkbox" class="lm-rtc-input" data-key="${esc(field.key)}" data-type="bool" ${field.value ? "checked" : ""}><span class="track"></span></label>`;
    } else {
      const step = meta.step != null ? meta.step : (field.type === "int" ? 1 : "any");
      const min = field.min != null ? ` min="${field.min}"` : "";
      const max = field.max != null ? ` max="${field.max}"` : "";
      control = `<input type="number" class="lm-rtc-input" data-key="${esc(field.key)}" data-type="${esc(field.type)}" value="${esc(field.value)}" step="${esc(step)}"${min}${max} style="width:120px">${unit}`;
    }
    return `<div class="ctrl-row">
      <div class="cl"><b>${esc(meta.label)}</b><small>${esc(meta.help)}</small></div>
      <div class="cr">${control}</div>
    </div>`;
  }

  function paintRuntimeConfig(data) {
    const fields = (data && data.fields) || [];
    _rtcServer = (data && data.values) || {};
    const host = $("lm-rtc-rows");
    if (!host) return;
    host.innerHTML = fields.length
      ? fields.map(rtcRow).join("")
      : '<div class="muted" style="padding:8px 0">暂无可调项</div>';
  }

  async function loadRuntimeConfig() {
    const host = $("lm-rtc-rows");
    try {
      const data = await LMApi.adminGetRuntimeConfig();
      paintRuntimeConfig(data);
    } catch (ex) {
      if (host) host.innerHTML = `<div class="lm-err">${esc(ex.message)}</div>`;
    }
  }

  // Collect only the values that differ from what the server last returned.
  function rtcChanges() {
    const changes = {};
    document.querySelectorAll(".lm-rtc-input").forEach((el) => {
      const key = el.dataset.key;
      let val;
      if (el.dataset.type === "bool") {
        val = el.checked;
        if (Boolean(_rtcServer[key]) !== val) changes[key] = val;
      } else {
        val = el.dataset.type === "int" ? parseInt(el.value, 10) : parseFloat(el.value);
        if (!Number.isNaN(val) && Number(_rtcServer[key]) !== val) changes[key] = val;
      }
    });
    return changes;
  }

  // One 保存 commits both the concurrency limit (license) and the hot-reloadable
  // runtime config, then reloads so the ring / stepper / rows reflect the server
  // and the dirty state resets.
  const rtcSave = $("lm-rtc-save");
  if (rtcSave) rtcSave.addEventListener("click", async () => {
    const changes = rtcChanges();
    const licEl = $("lm-license-count");
    const licVal = licEl ? Math.max(1, Number(licEl.value) || 1) : null;
    const licChanged = licVal != null && licVal !== _licServer;
    if (!Object.keys(changes).length && !licChanged) {
      toast("没有需要保存的修改", true); return;
    }
    try {
      if (licChanged) await LMApi.adminSetLicense(licVal);
      if (Object.keys(changes).length) await LMApi.adminSetRuntimeConfig(changes);
      await loadLicense();
      toast("已保存并生效", true);
    } catch (ex) { toast(ex.message || "保存失败", false); }
  });
  const rtcReset = $("lm-rtc-reset");
  if (rtcReset) rtcReset.addEventListener("click", loadLicense);

  // --- tasks --------------------------------------------------------------- //
  async function loadTasks() {
    const tb = $("lm-admin-task-rows");
    try {
      const data = await LMApi.adminListTasks();
      const tasks = data.tasks || [];
      const running = tasks.filter((t) =>
        String(t.status || "").toLowerCase() === "running").length;
      const kr = $("lm-kpi-running"); if (kr) kr.textContent = running;
      const krt = $("lm-kpi-run-trend");
      if (krt) { krt.textContent = running ? "运行中" : "空闲"; krt.className = "trend " + (running ? "up" : "flat"); }
      tb.innerHTML = tasks.map((t) => {
        const cancel = FINAL.includes(t.status)
          ? "" : `<button class="btn small lm-atask-cancel" data-k="${esc(t.task_id)}">取消</button>`;
        return `<tr>
          <td><code>${esc(t.task_id)}</code></td>
          <td>${esc(t.project_code || (t.project_id ? "#" + t.project_id : "（未归属）"))}</td>
          <td>${esc(t.test_id)}</td>
          <td>${esc(t.submitter)}</td>
          <td>${mergedBadge(t)}</td>
          <td>${progCell(t.progress || 0)}</td>
          <td><code style="font-size:12px">${esc(fmtFinished(t))}</code></td>
          <td><div class="row-acts">${cancel}
            <button class="btn small danger lm-atask-del" data-k="${esc(t.task_id)}">删除</button></div></td>
        </tr>`;
      }).join("") || '<tr><td colspan="8" class="muted">暂无任务</td></tr>';
      tb.querySelectorAll(".lm-atask-cancel").forEach((b) =>
        b.addEventListener("click", () => cancelTask(b.dataset.k)));
      tb.querySelectorAll(".lm-atask-del").forEach((b) =>
        b.addEventListener("click", () => delTask(b.dataset.k)));
    } catch (ex) {
      tb.innerHTML = `<tr><td colspan="8" class="muted">${esc(ex.message)}</td></tr>`;
    }
  }
  async function cancelTask(key) {
    try { await LMApi.adminCancelTask(key); toast("已请求取消", true); loadTasks(); }
    catch (ex) { toast(ex.message, false); }
  }
  async function delTask(key) {
    if (!(await LMUI.confirm({
      level: "danger",
      title: "删除该任务",
      body: `任务 ${key} 及其工作区将被删除，此操作不可撤销。`,
      confirmText: "删除",
    }))) return;
    try { await LMApi.adminDeleteTask(key); toast("任务已删除", true); loadTasks(); }
    catch (ex) { toast(ex.message, false); }
  }
  $("lm-admin-tasks-refresh").addEventListener("click", loadTasks);
  async function pruneEvents() {
    if (!(await LMUI.confirm({
      level: "danger",
      title: "清理历史事件日志",
      body: "将仅保留各任务最近的记录，进行中的任务不受影响。操作不可撤销。",
      confirmText: "清理",
    }))) return;
    const btn = $("lm-admin-tasks-prune");
    if (btn) btn.disabled = true;
    try {
      const r = await LMApi.adminPruneTaskEvents();
      toast(`已清理 ${r.pruned_tasks || 0} 个任务，删除 ${r.deleted || 0} 条事件`, true);
    } catch (ex) { toast(ex.message || "清理失败", false); }
    finally { if (btn) btn.disabled = false; }
  }
  const pruneBtn = $("lm-admin-tasks-prune");
  if (pruneBtn) pruneBtn.addEventListener("click", pruneEvents);

  (window.LMReady || Promise.resolve()).then(() => {
    // The header KPIs are derived from the users and license payloads, so both
    // are fetched on every load regardless of which tab is showing.
    loadUsers();
    loadLicense();
    loadStats();
    const initial = window.LMUrl ? LMUrl.get("tab", DEFAULT_TAB) : DEFAULT_TAB;
    if (initial !== DEFAULT_TAB) {
      // users/license just fetched above; only 任务 needs a load of its own.
      activateTab(initial, { load: initial === "tasks" });
    }
  });
})();
