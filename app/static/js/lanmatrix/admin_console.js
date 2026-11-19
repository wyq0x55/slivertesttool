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

  function svgIcon(pathD) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", pathD);
    svg.appendChild(path);
    return svg;
  }

  function emptyRow(colspan, cls, text) {
    return el("tr", null, [
      el("td", { colSpan: colspan, className: cls, text: text }),
    ]);
  }

  // Deterministic gradient avatar (2-char label) — mirrors the design demo.
  function avatar(label, i) {
    const h1 = (i * 67) % 360, h2 = (i * 67 + 40) % 360;
    return el("span", {
      className: "avatar",
      style: `width:30px;height:30px;font-size:11px;background:linear-gradient(135deg,hsl(${h1} 55% 62%),hsl(${h2} 55% 50%))`,
      text: label,
    });
  }
  const PATH_EDIT = "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z";
  const PATH_DEL = "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6";

  // Merge execution ``status`` + judge ``result`` into one label, splitting a
  // failed run into a genuine ``failed`` (verdict FAIL) vs an ``error`` (ERROR).
  const STATUS_ZH = LMPill.TASK_ZH;
  function pillNode(status, label, tip) {
    const span = document.createElement("span");
    LMPill.apply(span, status, label, tip);
    return span;
  }
  function mergedBadge(t) {
    const st = String(t.status || "").toLowerCase();
    let cls = st || "notask";
    let label = st || "—";
    if (st === "failed") {
      const v = String(t.result || "").trim().toUpperCase();
      if (v.startsWith("ERROR")) { cls = "error"; label = "error"; }
    }
    return pillNode(cls, STATUS_ZH[label] || label, t.result || t.status || "");
  }
  function progCell(p) {
    p = p || 0;
    return el("div", { style: "display:flex;align-items:center;gap:8px" }, [
      el("div", { className: "prog", style: "width:70px;flex:0 0 auto" }, [
        el("i", { style: `width:${p}%` }),
      ]),
      el("span", { className: "muted", style: "font-size:12px", text: `${p}%` }),
    ]);
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
  const tabEls = Array.from(root.querySelectorAll(".tab"));
  const tabList = root.querySelector('[role="tablist"]');

  // The roving tab stop marks the one tab reachable with Tab. It follows focus
  // while arrowing, and returns to the selected tab when focus leaves the group.
  function setTabStop(target) {
    tabEls.forEach((tab) => { tab.tabIndex = tab === target ? 0 : -1; });
  }

  // Single source of truth for "show tab X": both the click handler and the
  // URL restore path go through here, so they can never drift apart.
  function activateTab(name, opts) {
    opts = opts || {};
    if (!TABS.includes(name)) name = DEFAULT_TAB;
    const focused = document.activeElement;
    const panes = Array.from(root.querySelectorAll(".pane"));
    tabEls.forEach((t) => {
      const on = t.dataset.tab === name;
      t.classList.toggle("on", on);
      t.setAttribute("aria-selected", String(on));
    });
    const selected = tabEls.find((t) => t.dataset.tab === name);
    // Selecting moves the roving stop onto the shown tab. Follow focus when it
    // is still on another tab, or inside a pane this selection is about to
    // hide — otherwise that control is left at tabindex=-1, or inside hidden.
    setTabStop(selected);
    if (selected && tabList && (tabList.contains(focused) || panes.some((pane) =>
        pane.dataset.pane !== name && pane.contains && pane.contains(focused)))) {
      selected.focus();
    }
    panes.forEach((p) => {
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

  tabEls.forEach((tab, index) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab, { push: true }));
    // Selection by activation: arrows move focus only, because selecting a tab
    // rewrites `?tab=` and starts a data load. Enter/Space activate the focused
    // tab through the click handler above (native button behavior).
    tab.addEventListener("keydown", (event) => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      let nextIndex;
      switch (event.key) {
        case "ArrowLeft": nextIndex = (index + tabEls.length - 1) % tabEls.length; break;
        case "ArrowRight": nextIndex = (index + 1) % tabEls.length; break;
        case "Home": nextIndex = 0; break;
        case "End": nextIndex = tabEls.length - 1; break;
        default: return;
      }
      event.preventDefault();
      setTabStop(tabEls[nextIndex]);
      tabEls[nextIndex].focus();
    });
  });

  // Roving tabindex follows focus while arrowing, so put it back on the selected
  // tab when focus leaves the group — Tab re-entry must land on the shown panel.
  if (tabList) {
    tabList.addEventListener("focusout", (event) => {
      if (!tabList.contains(event.relatedTarget)) {
        setTabStop(tabEls.find((t) => t.getAttribute("aria-selected") === "true"));
      }
    });
  }

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
      const rows = users.map((u, i) => {
        const active = u.status === "active";
        const name = u.display_name || u.username || "?";
        const projects = Array.isArray(u.projects) ? u.projects : [];
        const chips = projects.length
          ? el("div", { className: "projchips" }, projects.map((p) =>
            el("span", { className: "pchip", text: String(p == null ? "" : p) })))
          : el("span", { className: "muted", text: "—" });
        const sw = el("input", {
          type: "checkbox",
          className: "lm-user-admin-sw",
          dataset: { id: String(u.id) },
          checked: !!u.is_system_admin,
        });
        sw.addEventListener("change", () => toggleAdmin(Number(sw.dataset.id), sw.checked, sw));
        const editBtn = el("button", {
          type: "button",
          className: "btn small btn-icon lm-user-edit",
          dataset: { id: String(u.id) },
          title: "编辑",
        }, [svgIcon(PATH_EDIT)]);
        editBtn.addEventListener("click", () => openUser(Number(editBtn.dataset.id)));
        const delBtn = el("button", {
          type: "button",
          className: "btn small btn-icon danger lm-user-del",
          dataset: { id: String(u.id) },
          title: "删除",
        }, [svgIcon(PATH_DEL)]);
        delBtn.addEventListener("click", () => delUser(Number(delBtn.dataset.id)));
        return el("tr", null, [
          el("td", null, [el("div", { className: "u-name" }, [
            avatar(name.slice(0, 2), i),
            el("div", null, [
              el("b", { text: u.username || "" }),
              el("span", { className: "sub", text: u.display_name || "" }),
            ]),
          ])]),
          el("td", { className: "mono", text: u.email || "" }),
          el("td", null, [pillNode(active ? "on" : "off", active ? "启用" : "停用")]),
          el("td", null, [el("label", { className: "sw" }, [sw, el("span", { className: "track" })])]),
          el("td", null, [chips]),
          el("td", null, [el("div", { className: "row-acts" }, [editBtn, delBtn])]),
        ]);
      });
      tb.replaceChildren(...(rows.length ? rows : [emptyRow(6, "muted", "暂无账号")]));
      window._lmUsers = users;
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      tb.replaceChildren(emptyRow(6, "lm-err", ex.message == null ? "" : String(ex.message)));
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
    silver_max_runs_per_instance: {
      label: "单实例最大复用轮次",
      help: "单个预热 Silver 进程累计执行达到该次数后自动优雅退休重建（0 表示不限）",
      unit: "次", step: 5,
    },
    silver_clean_room: {
      label: "车规洁净室模式 (Clean-Room)",
      help: "启用后每次测试均分配全新冷启动 Silver 进程并在结束后销毁，消除跨用例 C/DLL 全局状态残留（ASPICE 正式签核推荐）",
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
    silver_max_csv_bytes: {
      label: "单次运行 output.csv 熔断上限",
      help: "缺失信号白名单时防止全量录制撑爆磁盘的字节上限（0 表示不限制）",
      unit: "Byte", step: 16777216,
    },
    task_event_retention: {
      label: "任务事件保留条数",
      help: "每个任务最多保留的历史事件（日志/进度）条数；每日凌晨自动清理，也可在任务管理中手动清理",
      unit: "条", step: 100,
    },
    curve_max_file_mb: {
      label: "变量曲线 · 最大 output.csv",
      help: "超过此大小的 output.csv 不在变量曲线中解析（仍可通过运行报告下载）",
      unit: "MiB", step: 1,
    },
    curve_point_limit: {
      label: "变量曲线 · 精确点上限",
      help: "单个变量在当前区间内的跳变点超过此数时，改为按像素发送最小/最大包络，并在图上标注",
      unit: "点", step: 500,
    },
    curve_max_lanes: {
      label: "变量曲线 · 最大泳道数",
      help: "同时显示的变量（泳道）上限，也是保存视图的上限",
      unit: "条", step: 1,
    },
    curve_cache_mb: {
      label: "变量曲线 · 索引缓存上限",
      help: "已解析 output.csv 的磁盘索引缓存总大小，超出后按最久未用淘汰",
      unit: "MiB", step: 16,
    },
  };
  // The last values fetched from the server, used to compute the dirty set.
  let _rtcServer = {};

  function rtcRow(field) {
    const meta = RTC_META[field.key] || { label: field.key, help: "" };
    const crChildren = [];
    if (field.type === "bool") {
      crChildren.push(el("label", { className: "sw" }, [
        el("input", {
          type: "checkbox",
          className: "lm-rtc-input",
          dataset: { key: String(field.key), type: "bool" },
          checked: !!field.value,
        }),
        el("span", { className: "track" }),
      ]));
    } else {
      const step = meta.step != null ? meta.step : (field.type === "int" ? 1 : "any");
      const input = el("input", {
        type: "number",
        className: "lm-rtc-input",
        dataset: { key: String(field.key), type: String(field.type) },
        value: field.value == null ? "" : String(field.value),
        step: String(step),
        style: "width:120px",
      });
      if (field.min != null) input.min = String(field.min);
      if (field.max != null) input.max = String(field.max);
      crChildren.push(input);
      if (meta.unit) {
        crChildren.push(document.createTextNode(" "));
        crChildren.push(el("span", { className: "muted", style: "font-size:12px", text: meta.unit }));
      }
    }
    return el("div", { className: "ctrl-row" }, [
      el("div", { className: "cl" }, [
        el("b", { text: meta.label }),
        el("small", { text: meta.help }),
      ]),
      el("div", { className: "cr" }, crChildren),
    ]);
  }

  function paintRuntimeConfig(data) {
    const fields = (data && data.fields) || [];
    _rtcServer = (data && data.values) || {};
    const host = $("lm-rtc-rows");
    if (!host) return;
    const children = fields.length
      ? fields.map(rtcRow)
      : [el("div", { className: "muted", style: "padding:8px 0", text: "暂无可调项" })];
    host.replaceChildren(...children);
  }

  async function loadRuntimeConfig() {
    const host = $("lm-rtc-rows");
    try {
      const data = await LMApi.adminGetRuntimeConfig();
      paintRuntimeConfig(data);
    } catch (ex) {
      if (host) host.replaceChildren(el("div", { className: "lm-err", text: ex.message == null ? "" : String(ex.message) }));
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
      const rows = tasks.map((t) => {
        const taskId = String(t.task_id == null ? "" : t.task_id);
        const acts = [];
        if (!FINAL.includes(t.status)) {
          const cancelBtn = el("button", {
            type: "button",
            className: "btn small lm-atask-cancel",
            dataset: { k: taskId },
            text: "取消",
          });
          cancelBtn.addEventListener("click", () => cancelTask(cancelBtn.dataset.k));
          acts.push(cancelBtn);
        }
        const delBtn = el("button", {
          type: "button",
          className: "btn small danger lm-atask-del",
          dataset: { k: taskId },
          text: "删除",
        });
        delBtn.addEventListener("click", () => delTask(delBtn.dataset.k));
        acts.push(delBtn);
        return el("tr", null, [
          el("td", null, [el("code", { text: taskId })]),
          el("td", { text: t.project_code || (t.project_id ? "#" + t.project_id : "（未归属）") }),
          el("td", { text: t.test_id == null ? "" : String(t.test_id) }),
          el("td", { text: t.submitter == null ? "" : String(t.submitter) }),
          el("td", null, [mergedBadge(t)]),
          el("td", null, [progCell(t.progress || 0)]),
          el("td", null, [el("code", { style: "font-size:12px", text: fmtFinished(t) })]),
          el("td", null, [el("div", { className: "row-acts" }, acts)]),
        ]);
      });
      tb.replaceChildren(...(rows.length ? rows : [emptyRow(8, "muted", "暂无任务")]));
    } catch (ex) {
      tb.replaceChildren(emptyRow(8, "muted", ex.message == null ? "" : String(ex.message)));
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
