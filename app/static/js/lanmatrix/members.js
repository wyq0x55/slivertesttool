/* Project member management: list members, add via candidate search, change
 * role, remove. The API enforces project.members (project admins / system
 * admins); non-admins get a read-only view and 403s on writes. */
(function () {
  "use strict";
  const root = document.querySelector(".lm-members");
  if (!root) return;
  const pid = Number(root.dataset.project);
  const rowsEl = document.getElementById("lm-member-rows");
  const addBox = document.getElementById("lm-member-add");
  const toggleBtn = document.getElementById("lm-member-toggle");
  const searchEl = document.getElementById("lm-mem-search");
  const roleEl = document.getElementById("lm-mem-role");
  const candEl = document.getElementById("lm-mem-candidates");
  let roles = ["project_admin", "editor", "reviewer", "reader"];
  let canManage = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  const ROLE_ZH = { project_admin: "管理员", editor: "编辑", reviewer: "评审", reader: "只读" };
  function roleText(r) { return ROLE_ZH[r] ? `${r}（${ROLE_ZH[r]}）` : r; }
  // Demo-style gradient avatar: 1–2 char initials over an index-derived hue.
  function initials(name) {
    const s = String(name || "?").trim();
    if (/[\u4e00-\u9fff]/.test(s)) return esc(s.slice(0, 2));
    const parts = s.split(/[\s._-]+/).filter(Boolean);
    return esc(((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase());
  }
  function avatar(name, i, extra) {
    const h1 = (i * 67) % 360, h2 = (i * 67 + 40) % 360;
    return `<span class="avatar"${extra ? ' style="' + extra + ';' : ' style="'}` +
      `background:linear-gradient(135deg,hsl(${h1} 55% 62%),hsl(${h2} 55% 50%))">${initials(name)}</span>`;
  }
  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3200);
  }

  const ROLE_TAG = { project_admin: "admin", editor: "editor", reviewer: "reviewer", reader: "reader" };
  function roleOptions(current) {
    return roles.map((r) =>
      `<option value="${r}"${r === current ? " selected" : ""}>${roleText(r)}</option>`).join("");
  }

  function renderMembers(members) {
    if (!members.length) {
      rowsEl.innerHTML = '<div class="state" style="grid-column:1/-1">' +
        '<h3>暂无成员</h3><p>添加成员以协作该项目。</p></div>';
      return;
    }
    rowsEl.innerHTML = members.map((m, i) => `
      <div class="mcard" data-id="${m.id}">
        <div class="top">
          ${avatar(m.display_name || m.username, i + 2, "width:42px;height:42px;font-size:15px;flex:0 0 42px")}
          <div class="who"><b>${esc(m.username)}</b><span>${esc(m.display_name || m.username)}</span></div>
          <span style="flex:1"></span>
          <span class="tag tag-role-${ROLE_TAG[m.role] || "reader"}">${esc(ROLE_ZH[m.role] || m.role)}</span>
        </div>
        <div class="foot">
          ${canManage
            ? `<select class="roleselect lm-role-sel" data-id="${m.id}">${roleOptions(m.role)}</select>`
            : `<span class="tag tag-role-${ROLE_TAG[m.role] || "reader"}">${esc(ROLE_ZH[m.role] || m.role)}</span>`}
          ${canManage
            ? `<button class="btn btn-sm btn-danger lm-mem-del" data-id="${m.id}">移除</button>`
            : ""}
        </div>
      </div>`).join("");
    if (canManage) {
      rowsEl.querySelectorAll(".lm-role-sel").forEach((s) =>
        s.addEventListener("change", () => changeRole(Number(s.dataset.id), s.value)));
      rowsEl.querySelectorAll(".lm-mem-del").forEach((b) =>
        b.addEventListener("click", () => removeMember(Number(b.dataset.id))));
    }
  }

  async function load() {
    try {
      const data = await LMApi.listMembers(pid);
      if (Array.isArray(data.roles) && data.roles.length) roles = data.roles;
      // System admins can always manage members — reveal the button up front so
      // it never depends on the candidate probe's timing.
      if (window.LM && LM.user && LM.user.is_system_admin && toggleBtn) {
        canManage = true; toggleBtn.hidden = false;
      }
      renderMembers(data.members || []);
      // Probe write permission by asking for candidates; 403 -> read-only.
      try {
        await LMApi.memberCandidates(pid, "");
        canManage = true;
        if (toggleBtn) toggleBtn.hidden = false;
        renderMembers(data.members || []);
        // Pre-fetch the candidate list so it is ready the moment the admin
        // opens the "添加成员" panel (which stays hidden until then).
        runSearch();
      } catch (ex) {
        canManage = false;
      }
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      rowsEl.innerHTML = `<div class="state err"><b>加载失败</b><span>${esc(ex.message)}</span></div>`;
    }
  }

  let searchTimer = null;
  function onSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 250);
  }
  async function runSearch() {
    const q = searchEl.value.trim();
    try {
      const data = await LMApi.memberCandidates(pid, q);
      const list = data.candidates || [];
      if (!list.length) {
        candEl.innerHTML = `<span class="muted" style="font-size:12.5px">${
          q ? "无匹配用户" : "没有可添加的用户（其余用户均已是成员）"}</span>`;
        return;
      }
      candEl.innerHTML = list.map((u, i) => `
        <span class="cand lm-cand" data-uid="${u.id}">
          ${avatar(u.display_name || u.username, i + 8, "width:22px;height:22px;font-size:9px;flex:0 0 22px")}
          ${esc(u.username)}${u.display_name ? " · " + esc(u.display_name) : ""}
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
        </span>`).join("");
      candEl.querySelectorAll(".lm-cand").forEach((b) =>
        b.addEventListener("click", () => addMember(Number(b.dataset.uid))));
    } catch (ex) {
      candEl.innerHTML = `<span class="muted" style="font-size:12.5px">${esc(ex.message)}</span>`;
    }
  }

  async function addMember(uid) {
    try {
      await LMApi.addMember(pid, { user_id: uid, role: roleEl.value });
      toast("成员已添加", true);
      searchEl.value = "";
      candEl.innerHTML = "";
      load();
    } catch (ex) { toast(ex.message, false); }
  }
  async function changeRole(id, role) {
    try {
      await LMApi.patchMember(pid, id, role);
      toast("角色已更新", true);
    } catch (ex) { toast(ex.message, false); load(); }
  }
  async function removeMember(id) {
    if (!(await LMUI.confirm({
      level: "danger",
      title: "移除该成员",
      body: "该成员将失去此项目的访问权限，可稍后重新添加。",
      confirmText: "移除",
    }))) return;
    try {
      await LMApi.removeMember(pid, id);
      toast("成员已移除", true);
      load();
    } catch (ex) { toast(ex.message, false); }
  }

  searchEl.addEventListener("input", onSearch);
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      const open = addBox.hidden;
      addBox.hidden = !open;
      if (open) { searchEl.focus(); runSearch(); }
    });
  }
  (window.LMReady || Promise.resolve()).then(load);
})();
