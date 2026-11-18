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
  const SVG_NS = "http://www.w3.org/2000/svg";

  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null) continue;
        if (k === "className") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "style") node.setAttribute("style", v);
        else if (k === "dataset") {
          for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
        } else {
          node[k] = v;
        }
      }
    }
    if (children) node.append(...children);
    return node;
  }
  function plusIcon() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("width", "13");
    svg.setAttribute("height", "13");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M12 5v14M5 12h14");
    svg.appendChild(path);
    return svg;
  }
  const ROLE_ZH = { project_admin: "管理员", editor: "编辑", reviewer: "评审", reader: "只读" };
  function roleText(r) { return ROLE_ZH[r] ? `${r}（${ROLE_ZH[r]}）` : r; }
  // Demo-style gradient avatar: 1–2 char initials over an index-derived hue.
  function initials(name) {
    const s = String(name || "?").trim();
    if (/[\u4e00-\u9fff]/.test(s)) return s.slice(0, 2);
    const parts = s.split(/[\s._-]+/).filter(Boolean);
    return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
  }
  function avatar(name, i, extra) {
    const h1 = (i * 67) % 360, h2 = (i * 67 + 40) % 360;
    const bg = `background:linear-gradient(135deg,hsl(${h1} 55% 62%),hsl(${h2} 55% 50%))`;
    return el("span", {
      className: "avatar",
      style: extra ? `${extra};${bg}` : bg,
      text: initials(name),
    });
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
    return roles.map((r) => el("option", {
      value: r,
      selected: r === current,
      text: roleText(r),
    }));
  }

  function renderMembers(members) {
    if (!members.length) {
      rowsEl.replaceChildren(el("div", { className: "state", style: "grid-column:1/-1" }, [
        el("h3", { text: "暂无成员" }),
        el("p", { text: "添加成员以协作该项目。" }),
      ]));
      return;
    }
    const cards = members.map((m, i) => {
      const roleCls = `tag tag-role-${ROLE_TAG[m.role] || "reader"}`;
      const roleLabel = ROLE_ZH[m.role] || m.role || "";
      const footChildren = [];
      if (canManage) {
        const sel = el("select", {
          className: "roleselect lm-role-sel",
          dataset: { id: String(m.id) },
        }, roleOptions(m.role));
        sel.addEventListener("change", () => changeRole(Number(sel.dataset.id), sel.value));
        const delBtn = el("button", {
          type: "button",
          className: "btn btn-sm btn-danger lm-mem-del",
          dataset: { id: String(m.id) },
          text: "移除",
        });
        delBtn.addEventListener("click", () => removeMember(Number(delBtn.dataset.id)));
        footChildren.push(sel, delBtn);
      } else {
        footChildren.push(el("span", { className: roleCls, text: roleLabel }));
      }
      return el("div", { className: "mcard", dataset: { id: String(m.id) } }, [
        el("div", { className: "top" }, [
          avatar(m.display_name || m.username, i + 2, "width:42px;height:42px;font-size:15px;flex:0 0 42px"),
          el("div", { className: "who" }, [
            el("b", { text: m.username || "" }),
            el("span", { text: m.display_name || m.username || "" }),
          ]),
          el("span", { style: "flex:1" }),
          el("span", { className: roleCls, text: roleLabel }),
        ]),
        el("div", { className: "foot" }, footChildren),
      ]);
    });
    rowsEl.replaceChildren(...cards);
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
      rowsEl.replaceChildren(el("div", { className: "state err" }, [
        el("b", { text: "加载失败" }),
        el("span", { text: ex.message == null ? "" : String(ex.message) }),
      ]));
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
        candEl.replaceChildren(el("span", {
          className: "muted",
          style: "font-size:12.5px",
          text: q ? "无匹配用户" : "没有可添加的用户（其余用户均已是成员）",
        }));
        return;
      }
      const items = list.map((u, i) => {
        const label = `${u.username || ""}${u.display_name ? " · " + u.display_name : ""}`;
        const chip = el("span", { className: "cand lm-cand", dataset: { uid: String(u.id) } }, [
          avatar(u.display_name || u.username, i + 8, "width:22px;height:22px;font-size:9px;flex:0 0 22px"),
          document.createTextNode(label),
          plusIcon(),
        ]);
        chip.addEventListener("click", () => addMember(Number(chip.dataset.uid)));
        return chip;
      });
      candEl.replaceChildren(...items);
    } catch (ex) {
      candEl.replaceChildren(el("span", {
        className: "muted",
        style: "font-size:12.5px",
        text: ex.message == null ? "" : String(ex.message),
      }));
    }
  }

  async function addMember(uid) {
    try {
      await LMApi.addMember(pid, { user_id: uid, role: roleEl.value });
      toast("成员已添加", true);
      searchEl.value = "";
      candEl.replaceChildren();
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
