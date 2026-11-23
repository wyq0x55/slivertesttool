/* Projects list: card grid with skeleton / empty / error states,
   client-side search + status filter, create blank, import-as-new. */
(function () {
  "use strict";
  const gridEl  = document.getElementById("lm-project-rows");
  const skelEl  = document.getElementById("lm-project-skel");
  const emptyEl = document.getElementById("lm-project-empty");
  const errEl   = document.getElementById("lm-project-err");
  const errMsg  = document.getElementById("lm-project-err-msg");
  const dialog  = document.getElementById("lm-new-dialog");
  const searchEl = document.getElementById("lm-proj-search");
  const segEl   = document.getElementById("lm-proj-seg");

  let all = [];
  let filter = { q: "", s: "all" };

  const STATUS_ZH = LMPill.PROJECT_ZH;
  const SVG_NS = "http://www.w3.org/2000/svg";

  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null) continue;
        if (k === "className") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "style") node.setAttribute("style", v);
        else if (k === "ariaLabel") node.setAttribute("aria-label", v);
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
  function fmtDate(s) {
    if (!s) return "—";
    const d = new Date(s);
    if (isNaN(d)) return String(s).replace("T", " ").replace("Z", "");
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3000);
  }
  function show(el, on) { if (el) el.hidden = !on; }

  function skeleton() {
    const cards = Array.from({ length: 6 }, () => el("div", { className: "skcard" }, [
      el("div", { className: "row" }, [
        el("span", { className: "sk", style: "width:44px;height:20px" }),
        el("span", { className: "sk", style: "width:52px;height:16px" }),
      ]),
      el("span", { className: "sk", style: "width:70%;height:17px;display:block;margin-bottom:9px" }),
      el("span", { className: "sk", style: "width:100%;height:12px;display:block;margin-bottom:6px" }),
      el("span", { className: "sk", style: "width:85%;height:12px;display:block" }),
      el("div", {
        className: "row",
        style: "margin:18px 0 16px;padding:14px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)",
      }, [
        el("span", { className: "sk", style: "width:40px;height:28px" }),
        el("span", { className: "sk", style: "width:40px;height:28px" }),
        el("span", { className: "sk", style: "width:40px;height:28px" }),
      ]),
      el("div", { className: "row", style: "margin:0" }, [
        el("span", { className: "sk", style: "width:80px;height:26px;border-radius:999px" }),
        el("span", { className: "sk", style: "width:70px;height:12px" }),
      ]),
    ]));
    skelEl.replaceChildren(...cards);
  }

  function avatarStack(p) {
    const inits = Array.isArray(p.members) ? p.members : [];
    const extra = p.member_extra || 0;
    if (!inits.length && !extra) {
      // fall back to the raw member_count when no initials are supplied
      const n = p.member_count || 0;
      if (!n) return [];
      return [el("span", { className: "avatar more", text: String(n) })];
    }
    const nodes = inits.map((m, i) => {
      const hue1 = (i * 57) % 360, hue2 = (i * 57 + 40) % 360;
      return el("span", {
        className: "avatar",
        style: `background:linear-gradient(135deg,hsl(${hue1} 55% 62%),hsl(${hue2} 55% 50%))`,
        text: String(m == null ? "" : m),
      });
    });
    if (extra > 0) nodes.push(el("span", { className: "avatar more", text: `+${extra}` }));
    return nodes;
  }

  function metricBox(val, valClass, label) {
    return el("div", { className: "metric" }, [
      el("div", { className: valClass ? `v ${valClass}` : "v", text: String(val) }),
      el("div", { className: "k", text: label }),
    ]);
  }

  function searchGlyph() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("cx", "11");
    circle.setAttribute("cy", "11");
    circle.setAttribute("r", "7");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "m21 21-4.3-4.3");
    svg.append(circle, path);
    return svg;
  }

  function cardEl(p) {
    const isUt = p.project_type === "unit_test";
    const st = String(p.status || "draft");
    const stZh = STATUS_ZH[p.status] || st;
    const hasDesc = (p.description || "").trim().length > 0;
    const defaultDesc = isUt ? "暂无描述 · 点击进入实施一览" : "暂无描述 · 点击进入在线编辑";
    const desc = hasDesc ? String(p.description) : defaultDesc;
    const mainHref = isUt ? `/lanmatrix/projects/${p.id}/ut` : `/lanmatrix/projects/${p.id}`;
    const nameStr = p.name == null ? "" : String(p.name);
    const codeStr = p.code == null ? "" : String(p.code);
    const mainAria = isUt ? `打开 ${nameStr} 实施一览` : `打开 ${nameStr} 在线编辑`;
    // Every card names its type, so the two kinds read as peers in one list
    // rather than one of them looking like an exception.
    const typeTag = isUt
      ? el("span", { className: "ptype ptype-ut", text: "单元测试" })
      : el("span", { className: "ptype ptype-silver", text: "Silver" });

    let middleZone;
    if (isUt) {
      const needsHuman = p.ut_jobs_needs_human || 0;
      middleZone = el("a", {
        className: "zone zone-tasks",
        href: `/lanmatrix/projects/${p.id}/tasks`,
        ariaLabel: "查看单元测试作业",
      }, [
        el("div", { className: "metrics" }, [
          metricBox(p.ut_targets_count || 0, "", "函数"),
          metricBox(p.ut_jobs_approved || 0, "ok", "审核通过"),
          metricBox(needsHuman, needsHuman ? "warn" : "", "需人工"),
        ]),
      ]);
    } else {
      const total = p.task_total || 0;
      const passed = p.task_passed || 0;
      const failed = p.task_failed || 0;
      const rate = total ? Math.round((passed / total) * 100) : 0;
      middleZone = el("a", {
        className: "zone zone-tasks",
        href: `/lanmatrix/projects/${p.id}/tasks`,
        ariaLabel: "查看测试任务",
      }, [
        el("div", { className: "metrics" }, [
          metricBox(total, "", "测试项"),
          metricBox(total ? rate + "%" : "—", "ok", "通过率"),
          metricBox(failed, failed ? "warn" : "", "失败"),
        ]),
      ]);
    }

    const editBtn = el("button", {
      type: "button",
      className: "btn small",
      dataset: { edit: String(p.id) },
      text: "编辑",
    });
    editBtn.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      const card = editBtn.closest(".card");
      openEdit({
        id: card.getAttribute("data-id"),
        code: card.getAttribute("data-code"),
        name: card.getAttribute("data-name"),
        status: card.getAttribute("data-status"),
        description: card.getAttribute("data-desc"),
      });
    });
    const delBtn = el("button", {
      type: "button",
      className: "btn small danger",
      dataset: { del: String(p.id), code: codeStr, name: nameStr },
      text: "删除",
    });
    delBtn.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      onDelete(delBtn.getAttribute("data-del"),
        delBtn.getAttribute("data-code"),
        delBtn.getAttribute("data-name"));
    });

    return el("div", {
      className: "card zoned",
      dataset: {
        id: String(p.id),
        code: codeStr,
        name: nameStr,
        status: st,
        desc: p.description == null ? "" : String(p.description),
      },
    }, [
      el("a", { className: "zone zone-main", href: mainHref, ariaLabel: mainAria }, [
        el("div", { className: "card-top" }, [
          el("span", { className: "card-tags" }, [
            el("span", { className: "code-chip", text: codeStr }),
            typeTag,
          ]),
          el("span", { className: "status" }, [
            el("span", { className: `dot ${st}` }),
            document.createTextNode(stZh),
          ]),
        ]),
        el("h3", { text: nameStr }),
        el("p", { className: hasDesc ? "desc" : "desc empty", text: desc }),
      ]),
      middleZone,
      el("a", {
        className: "zone zone-members",
        href: `/lanmatrix/projects/${p.id}/members`,
        ariaLabel: "项目成员管理",
      }, [
        el("div", { className: "card-foot" }, [
          el("div", { className: "stack" }, avatarStack(p)),
          el("span", { className: "upd", text: `更新于 ${fmtDate(p.updated_at)}` }),
        ]),
      ]),
      el("div", { className: "card-acts" }, [editBtn, delBtn]),
    ]);
  }

  function render() {
    const q = filter.q.trim().toLowerCase();
    const list = all.filter((p) => {
      if (filter.s !== "all" && (p.status || "draft") !== filter.s) return false;
      if (!q) return true;
      return (p.code || "").toLowerCase().includes(q) ||
             (p.name || "").toLowerCase().includes(q);
    });
    if (!all.length) {
      show(gridEl, false); show(emptyEl, true); show(errEl, false);
      return;
    }
    show(emptyEl, false); show(errEl, false);
    if (!list.length) {
      show(gridEl, true);
      gridEl.replaceChildren(el("div", { className: "state", style: "grid-column:1/-1" }, [
        el("span", { className: "glyph" }, [searchGlyph()]),
        el("h3", { text: "没有匹配的项目" }),
        el("p", { text: "试试调整搜索或筛选条件。" }),
      ]));
      return;
    }
    show(gridEl, true);
    gridEl.replaceChildren(...list.map(cardEl));
  }

  async function load() {
    show(skelEl, true); show(gridEl, false); show(emptyEl, false); show(errEl, false);
    skeleton();
    try {
      const data = await LMApi.listProjects();
      all = data.projects || [];
      show(skelEl, false);
      render();
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      show(skelEl, false); show(gridEl, false); show(emptyEl, false); show(errEl, true);
      if (errMsg) errMsg.textContent = ex.message || "请稍后重试。";
    }
  }

  async function onDelete(id, code, name) {
    const label = `${code}${name ? " / " + name : ""}`;
    // Widest blast radius in the product: this cascades to every test item,
    // field, comment, task and audit record. Require the code to be retyped.
    const ok = await LMUI.confirm({
      level: "critical",
      title: `删除项目「${label}」`,
      body:
        "这将永久删除该项目及其所有关联数据（测试项、字段、评论、" +
        "任务、审计日志等），且无法恢复。",
      requireText: code,
      confirmText: "永久删除项目",
    });
    if (!ok) return;
    try {
      await LMApi.deleteProject(id);
      toast("项目已删除", true);
      load();
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      toast(ex.message || "删除失败", false);
    }
  }

  /* --- filters --- */
  if (searchEl) searchEl.addEventListener("input", () => { filter.q = searchEl.value; render(); });
  if (segEl) segEl.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-s]");
    if (!b) return;
    segEl.querySelectorAll("button").forEach((x) => {
      const selected = x === b;
      x.classList.toggle("on", selected);
      x.setAttribute("aria-pressed", String(selected));
    });
    filter.s = b.getAttribute("data-s");
    render();
  });

  /* --- new project dialog --- */
  function openNew() { document.getElementById("lm-np-error").textContent = ""; dialog.showModal(); }
  document.getElementById("lm-new-project").addEventListener("click", openNew);
  const emptyNew = document.getElementById("lm-empty-new");
  if (emptyNew) emptyNew.addEventListener("click", openNew);
  const retry = document.getElementById("lm-proj-retry");
  if (retry) retry.addEventListener("click", load);

  document.getElementById("lm-np-ok").addEventListener("click", async (e) => {
    e.preventDefault();
    const err = document.getElementById("lm-np-error");
    const typeEl = document.getElementById("lm-np-type");
    const ptype = typeEl ? typeEl.value : "silver";
    try {
      const data = await LMApi.createProject({
        project_type: ptype,
        code: document.getElementById("lm-np-code").value.trim(),
        name: document.getElementById("lm-np-name").value.trim(),
        description: document.getElementById("lm-np-desc").value.trim(),
      });
      dialog.close();
      window.location = ptype === "unit_test"
        ? `/lanmatrix/projects/${data.project.id}/ut`
        : `/lanmatrix/projects/${data.project.id}`;
    } catch (ex) {
      err.textContent = ex.message;
    }
  });

  /* --- edit project dialog --- */
  const editDialog = document.getElementById("lm-edit-dialog");
  function openEdit(p) {
    document.getElementById("lm-ep-id").value = p.id;
    document.getElementById("lm-ep-name").value = p.name || "";
    document.getElementById("lm-ep-desc").value = p.description || "";
    document.getElementById("lm-ep-status").value = p.status || "draft";
    const err = document.getElementById("lm-ep-error");
    if (err) err.textContent = "";
    editDialog.showModal();
  }
  const epOk = document.getElementById("lm-ep-ok");
  if (epOk) epOk.addEventListener("click", async (e) => {
    e.preventDefault();
    const err = document.getElementById("lm-ep-error");
    const id = document.getElementById("lm-ep-id").value;
    const changes = {
      name: document.getElementById("lm-ep-name").value.trim(),
      description: document.getElementById("lm-ep-desc").value.trim(),
      status: document.getElementById("lm-ep-status").value,
    };
    if (!changes.name) {
      err.textContent = "项目名称不能为空";
      return;
    }
    try {
      await LMApi.patchProject(id, changes);
      editDialog.close();
      const item = all.find((x) => String(x.id) === String(id));
      if (item) {
        item.name = changes.name;
        item.description = changes.description;
        item.status = changes.status;
        item.updated_at = new Date().toISOString();
      }
      render();
      toast("项目已更新", true);
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      err.textContent = ex.message || "更新失败";
    }
  });

  /* --- import as new --- */
  const fileInput = document.getElementById("lm-import-file");
  document.getElementById("lm-import-new").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const base = file.name.replace(/\.xlsx$/i, "");
    try {
      const proj = await LMApi.createProject({ code: base.toUpperCase().slice(0, 30), name: base });
      const pid = proj.project.id;
      const job = await LMApi.createImport(pid, file, "upsert");
      const jobId = job.job.id;
      if (job.job.preview && job.job.preview.invalid > 0) {
        toast(`导入含 ${job.job.preview.invalid} 行错误，请在项目内修正`, false);
      } else {
        await LMApi.commitImport(jobId);
        toast("导入成功", true);
      }
      window.location = `/lanmatrix/projects/${pid}`;
    } catch (ex) {
      toast(ex.message, false);
    }
    fileInput.value = "";
  });

  load();
})();
