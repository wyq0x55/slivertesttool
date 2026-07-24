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

  // Merge execution ``status`` + judge ``result`` into one label, splitting a
  // failed run into a genuine ``failed`` (verdict FAIL) vs an ``error`` (ERROR).
  function mergedBadge(t) {
    const st = String(t.status || "").toLowerCase();
    let cls = st || "notask";
    let label = st || "—";
    if (st === "failed") {
      const v = String(t.result || "").trim().toUpperCase();
      if (v.startsWith("ERROR")) { cls = "error"; label = "error"; }
    }
    return `<span class="lm-badge lm-status-${esc(cls)}" title="${esc(t.result || t.status || "")}">${esc(label)}</span>`;
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
  const loaders = { users: loadUsers, license: loadLicense, tasks: loadTasks };
  root.querySelectorAll(".lm-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      root.querySelectorAll(".lm-tab").forEach((t) => t.classList.remove("lm-active"));
      tab.classList.add("lm-active");
      const name = tab.dataset.tab;
      root.querySelectorAll(".lm-tabpane").forEach((p) => { p.hidden = p.dataset.pane !== name; });
      (loaders[name] || (() => {}))();
    });
  });

  // --- accounts ------------------------------------------------------------ //
  async function loadUsers() {
    const tb = $("lm-user-rows");
    try {
      const data = await LMApi.adminListUsers();
      const users = data.users || [];
      tb.innerHTML = users.map((u) => `
        <tr>
          <td><code>${esc(u.username)}</code></td>
          <td>${esc(u.display_name || "")}</td>
          <td>${esc(u.email || "")}</td>
          <td><span class="lm-badge lm-status-${u.status === "active" ? "passed" : "cancelled"}">${esc(u.status)}</span></td>
          <td>${u.is_system_admin ? "✔" : ""}</td>
          <td>${u.project_count || 0}</td>
          <td class="lm-row-actions">
            <button class="lm-btn lm-btn-sm lm-user-edit" data-id="${u.id}">编辑</button>
            <button class="lm-btn lm-btn-sm lm-btn-danger lm-user-del" data-id="${u.id}">删除</button>
          </td>
        </tr>`).join("") || '<tr><td colspan="7" class="lm-muted">暂无账号</td></tr>';
      window._lmUsers = users;
      tb.querySelectorAll(".lm-user-edit").forEach((b) =>
        b.addEventListener("click", () => openUser(Number(b.dataset.id))));
      tb.querySelectorAll(".lm-user-del").forEach((b) =>
        b.addEventListener("click", () => delUser(Number(b.dataset.id))));
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      tb.innerHTML = `<tr><td colspan="7" class="lm-err">${esc(ex.message)}</td></tr>`;
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
    if (!confirm("确定删除该账号？该操作会移除其所有项目成员身份。")) return;
    try { await LMApi.adminDeleteUser(id); toast("账号已删除", true); loadUsers(); }
    catch (ex) { toast(ex.message, false); }
  }

  // Model registration lives on the per-project 模型管理 page; it was removed
  // from the system console to avoid a duplicate, global surface.

  // --- license ------------------------------------------------------------- //
  async function loadLicense() {
    try {
      const data = await LMApi.adminGetLicense();
      const l = data.license || {};
      $("lm-license-info").textContent =
        `总量 ${l.total || 0}，使用中 ${l.in_use || 0}，空闲 ${l.available || 0}，排队 ${l.queued_jobs || 0}`;
      $("lm-license-count").value = l.total || 1;
    } catch (ex) { $("lm-license-info").textContent = ex.message; }
  }
  $("lm-license-save").addEventListener("click", async () => {
    try {
      await LMApi.adminSetLicense(Number($("lm-license-count").value));
      toast("已保存", true);
      loadLicense();
    } catch (ex) { toast(ex.message, false); }
  });

  // --- tasks --------------------------------------------------------------- //
  async function loadTasks() {
    const tb = $("lm-admin-task-rows");
    try {
      const data = await LMApi.adminListTasks();
      const tasks = data.tasks || [];
      tb.innerHTML = tasks.map((t) => {
        const cancel = FINAL.includes(t.status)
          ? "" : `<button class="lm-btn lm-btn-sm lm-atask-cancel" data-k="${esc(t.task_id)}">取消</button>`;
        return `<tr>
          <td><code>${esc(t.task_id)}</code></td>
          <td>${esc(t.project_code || (t.project_id ? "#" + t.project_id : "（未归属）"))}</td>
          <td>${esc(t.test_id)}</td>
          <td>${esc(t.submitter)}</td>
          <td>${mergedBadge(t)}</td>
          <td>${t.progress || 0}%</td>
          <td class="lm-cell-time"><code>${esc(fmtFinished(t))}</code></td>
          <td class="lm-row-actions">${cancel}
            <button class="lm-btn lm-btn-sm lm-btn-danger lm-atask-del" data-k="${esc(t.task_id)}">删除</button></td>
        </tr>`;
      }).join("") || '<tr><td colspan="8" class="lm-muted">暂无任务</td></tr>';
      tb.querySelectorAll(".lm-atask-cancel").forEach((b) =>
        b.addEventListener("click", () => cancelTask(b.dataset.k)));
      tb.querySelectorAll(".lm-atask-del").forEach((b) =>
        b.addEventListener("click", () => delTask(b.dataset.k)));
    } catch (ex) {
      tb.innerHTML = `<tr><td colspan="8" class="lm-err">${esc(ex.message)}</td></tr>`;
    }
  }
  async function cancelTask(key) {
    try { await LMApi.adminCancelTask(key); toast("已请求取消", true); loadTasks(); }
    catch (ex) { toast(ex.message, false); }
  }
  async function delTask(key) {
    if (!confirm("确定删除该任务及其工作区？")) return;
    try { await LMApi.adminDeleteTask(key); toast("任务已删除", true); loadTasks(); }
    catch (ex) { toast(ex.message, false); }
  }
  $("lm-admin-tasks-refresh").addEventListener("click", loadTasks);

  (window.LMReady || Promise.resolve()).then(loadUsers);
})();
