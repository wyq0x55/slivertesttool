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
  const loaders = { users: loadUsers, license: loadLicense, tasks: loadTasks };
  root.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      root.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t === tab));
      const name = tab.dataset.tab;
      root.querySelectorAll(".pane").forEach((p) => {
        const on = p.dataset.pane === name;
        p.classList.toggle("on", on);
        p.hidden = !on;
      });
      (loaders[name] || (() => {}))();
    });
  });

  // --- accounts ------------------------------------------------------------ //
  async function loadUsers() {
    const tb = $("lm-user-rows");
    try {
      const data = await LMApi.adminListUsers();
      const users = data.users || [];
      const kUsers = $("lm-kpi-users"); if (kUsers) kUsers.textContent = users.length;
      tb.innerHTML = users.map((u) => {
        const active = u.status === "active";
        const initial = esc((u.display_name || u.username || "?").slice(0, 1).toUpperCase());
        const role = u.is_system_admin
          ? '<span class="tag tag-role-admin">系统管理员</span>'
          : '<span class="tag tag-role-reader">成员</span>';
        return `
        <tr>
          <td><div class="u-name"><span class="avatar" style="width:26px;height:26px;font-size:11px">${initial}</span><code>${esc(u.username)}</code></div></td>
          <td>${esc(u.display_name || "")}</td>
          <td>${esc(u.email || "")}</td>
          <td>${pill(active ? "on" : "off", active ? "启用" : "停用")}</td>
          <td>${role}</td>
          <td>${u.project_count || 0}</td>
          <td><div class="row-acts">
            <button class="btn small lm-user-edit" data-id="${u.id}">编辑</button>
            <button class="btn small danger lm-user-del" data-id="${u.id}">删除</button>
          </div></td>
        </tr>`;
      }).join("") || '<tr><td colspan="7" class="muted">暂无账号</td></tr>';
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
  function paintLicense(l) {
    l = l || {};
    const total = l.total || 0, inUse = l.in_use || 0;
    const pct = total > 0 ? Math.round((inUse / total) * 100) : 0;
    const ring = $("lm-ring"); if (ring) ring.style.setProperty("--p", pct);
    const rp = $("lm-ring-pct"); if (rp) rp.textContent = pct + "%";
    const info = $("lm-license-info");
    if (info) info.textContent =
      `总量 ${total} · 使用中 ${inUse} · 空闲 ${l.available || 0} · 排队 ${l.queued_jobs || 0}`;
    const kt = $("lm-kpi-total"); if (kt) kt.textContent = total;
    const ki = $("lm-kpi-inuse"); if (ki) ki.textContent = inUse;
    const kq = $("lm-kpi-queued"); if (kq) kq.textContent = l.queued_jobs || 0;
    const cnt = $("lm-license-count"); if (cnt) cnt.value = total || 1;
  }
  async function loadLicense() {
    try {
      const data = await LMApi.adminGetLicense();
      paintLicense(data.license || {});
    } catch (ex) { const info = $("lm-license-info"); if (info) info.textContent = ex.message; }
  }
  function stepLicense(d) {
    const el = $("lm-license-count");
    el.value = Math.max(1, (Number(el.value) || 1) + d);
  }
  const licDec = $("lm-lic-dec"); if (licDec) licDec.addEventListener("click", () => stepLicense(-1));
  const licInc = $("lm-lic-inc"); if (licInc) licInc.addEventListener("click", () => stepLicense(1));
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
    if (!confirm("确定删除该任务及其工作区？")) return;
    try { await LMApi.adminDeleteTask(key); toast("任务已删除", true); loadTasks(); }
    catch (ex) { toast(ex.message, false); }
  }
  $("lm-admin-tasks-refresh").addEventListener("click", loadTasks);

  (window.LMReady || Promise.resolve()).then(() => { loadUsers(); loadLicense(); });
})();
