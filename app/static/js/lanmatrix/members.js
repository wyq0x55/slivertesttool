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
  const searchEl = document.getElementById("lm-mem-search");
  const roleEl = document.getElementById("lm-mem-role");
  const candEl = document.getElementById("lm-mem-candidates");
  let roles = ["project_admin", "editor", "reviewer", "reader"];
  let canManage = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3200);
  }

  function roleOptions(current) {
    return roles.map((r) =>
      `<option value="${r}"${r === current ? " selected" : ""}>${r}</option>`).join("");
  }

  function renderMembers(members) {
    if (!members.length) {
      rowsEl.innerHTML = '<tr><td colspan="4" class="lm-muted">暂无成员</td></tr>';
      return;
    }
    rowsEl.innerHTML = members.map((m) => `
      <tr data-id="${m.id}">
        <td><code>${esc(m.username)}</code></td>
        <td>${esc(m.display_name || m.username)}</td>
        <td>${canManage
          ? `<select class="lm-role-sel" data-id="${m.id}">${roleOptions(m.role)}</select>`
          : `<span class="lm-badge">${esc(m.role)}</span>`}</td>
        <td>${canManage
          ? `<button class="lm-btn lm-btn-sm lm-btn-danger lm-mem-del" data-id="${m.id}">移除</button>`
          : ""}</td>
      </tr>`).join("");
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
      renderMembers(data.members || []);
      // Probe write permission by asking for candidates; 403 -> read-only.
      try {
        await LMApi.memberCandidates(pid, "");
        canManage = true;
        addBox.hidden = false;
        renderMembers(data.members || []);
        // Show the full pick-list up front so admins can select any user
        // without having to type a query first.
        runSearch();
      } catch (ex) {
        canManage = false;
      }
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      rowsEl.innerHTML = `<tr><td colspan="4" class="lm-err">${esc(ex.message)}</td></tr>`;
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
        candEl.innerHTML = q
          ? '<span class="lm-muted">无匹配用户</span>'
          : '<span class="lm-muted">没有可添加的用户（其余用户均已是成员）</span>';
        return;
      }
      candEl.innerHTML = list.map((u) => `
        <button class="lm-btn lm-btn-sm lm-cand" data-uid="${u.id}">
          ${esc(u.username)} · ${esc(u.display_name)}
        </button>`).join("");
      candEl.querySelectorAll(".lm-cand").forEach((b) =>
        b.addEventListener("click", () => addMember(Number(b.dataset.uid))));
    } catch (ex) {
      candEl.innerHTML = `<span class="lm-err">${esc(ex.message)}</span>`;
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
    if (!confirm("确定移除该成员？")) return;
    try {
      await LMApi.removeMember(pid, id);
      toast("成员已移除", true);
      load();
    } catch (ex) { toast(ex.message, false); }
  }

  searchEl.addEventListener("input", onSearch);
  (window.LMReady || Promise.resolve()).then(load);
})();
