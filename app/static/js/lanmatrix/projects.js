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

  const STATUS_ZH = { draft: "草稿", active: "活跃", frozen: "冻结", archived: "归档" };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function fmtDate(s) {
    if (!s) return "—";
    const d = new Date(s);
    if (isNaN(d)) return esc(String(s).replace("T", " ").replace("Z", ""));
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
    const one = `<div class="skcard">
      <div class="row"><span class="sk" style="width:44px;height:20px"></span><span class="sk" style="width:52px;height:16px"></span></div>
      <span class="sk" style="width:70%;height:17px;display:block;margin-bottom:9px"></span>
      <span class="sk" style="width:100%;height:12px;display:block;margin-bottom:6px"></span>
      <span class="sk" style="width:85%;height:12px;display:block"></span>
      <div class="row" style="margin:18px 0 16px;padding:14px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)">
        <span class="sk" style="width:40px;height:28px"></span><span class="sk" style="width:40px;height:28px"></span><span class="sk" style="width:40px;height:28px"></span>
      </div>
      <div class="row" style="margin:0"><span class="sk" style="width:80px;height:26px;border-radius:999px"></span><span class="sk" style="width:70px;height:12px"></span></div>
    </div>`;
    skelEl.innerHTML = one.repeat(6);
  }

  function avatarStack(p) {
    const inits = Array.isArray(p.members) ? p.members : [];
    const extra = p.member_extra || 0;
    if (!inits.length && !extra) {
      // fall back to the raw member_count when no initials are supplied
      const n = p.member_count || 0;
      if (!n) return "";
      return `<span class="avatar more">${n}</span>`;
    }
    let h = inits.map((m, i) => {
      const hue1 = (i * 57) % 360, hue2 = (i * 57 + 40) % 360;
      return `<span class="avatar" style="background:linear-gradient(135deg,hsl(${hue1} 55% 62%),hsl(${hue2} 55% 50%))">${esc(m)}</span>`;
    }).join("");
    if (extra > 0) h += `<span class="avatar more">+${extra}</span>`;
    return h;
  }

  function cardHtml(p) {
    const st = esc(p.status || "draft");
    const stZh = STATUS_ZH[p.status] || st;
    const total = p.task_total || 0;
    const passed = p.task_passed || 0;
    const failed = p.task_failed || 0;
    const rate = total ? Math.round((passed / total) * 100) : 0;
    return `<div class="card" data-code="${esc(p.code)}" data-name="${esc(p.name)}" data-status="${st}">
      <a class="card-link" href="/lanmatrix/projects/${p.id}" aria-label="打开 ${esc(p.name)}"></a>
      <div class="card-top">
        <span class="code-chip">${esc(p.code)}</span>
        <span class="status"><span class="dot ${st}"></span>${esc(stZh)}</span>
      </div>
      <h3>${esc(p.name)}</h3>
      <p class="desc">${esc(p.description) || "暂无描述"}</p>
      <div class="metrics">
        <div class="metric"><div class="v">${total}</div><div class="k">测试项</div></div>
        <div class="metric"><div class="v ok">${total ? rate + "%" : "—"}</div><div class="k">通过率</div></div>
        <div class="metric"><div class="v ${failed ? "warn" : ""}">${failed}</div><div class="k">失败</div></div>
      </div>
      <div class="card-foot">
        <div class="stack">${avatarStack(p)}</div>
        <span class="upd">更新于 ${fmtDate(p.updated_at)}</span>
      </div>
      <div class="card-acts">
        <a class="btn small" href="/lanmatrix/projects/${p.id}/tasks">任务</a>
        <a class="btn small" href="/lanmatrix/projects/${p.id}/members">成员</a>
        <button class="btn small danger" data-del="${p.id}"
          data-code="${esc(p.code)}" data-name="${esc(p.name)}">删除</button>
      </div>
    </div>`;
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
      gridEl.innerHTML = `<div class="state" style="grid-column:1/-1">
        <span class="glyph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></span>
        <h3>没有匹配的项目</h3><p>试试调整搜索或筛选条件。</p></div>`;
      return;
    }
    show(gridEl, true);
    gridEl.innerHTML = list.map(cardHtml).join("");
    gridEl.querySelectorAll("button[data-del]").forEach((btn) => {
      btn.addEventListener("click", () => onDelete(
        btn.getAttribute("data-del"),
        btn.getAttribute("data-code"),
        btn.getAttribute("data-name")));
    });
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
    if (!window.confirm(
        `确定要删除项目「${label}」吗？\n\n` +
        `这将永久删除该项目及其所有关联数据（测试项、字段、评论、` +
        `任务、审计日志等），且无法恢复。`)) {
      return;
    }
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
    segEl.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    filter.s = b.getAttribute("data-s");
    render();
  });

  /* --- new project dialog --- */
  function openNew() { document.getElementById("lm-np-error").hidden = true; dialog.showModal(); }
  document.getElementById("lm-new-project").addEventListener("click", openNew);
  const emptyNew = document.getElementById("lm-empty-new");
  if (emptyNew) emptyNew.addEventListener("click", openNew);
  const retry = document.getElementById("lm-proj-retry");
  if (retry) retry.addEventListener("click", load);

  document.getElementById("lm-np-ok").addEventListener("click", async (e) => {
    e.preventDefault();
    const err = document.getElementById("lm-np-error");
    try {
      const data = await LMApi.createProject({
        code: document.getElementById("lm-np-code").value.trim(),
        name: document.getElementById("lm-np-name").value.trim(),
        description: document.getElementById("lm-np-desc").value.trim(),
      });
      dialog.close();
      window.location = `/lanmatrix/projects/${data.project.id}`;
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
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
