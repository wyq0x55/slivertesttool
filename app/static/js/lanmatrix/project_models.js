/* Per-project model management (card layout, mirrors member management):
 * list the project's .sil models as cards, register a server-side .sil path,
 * or upload a dll + sbs + pdb bundle (the server generates an empty .sil whose
 * only module line is "<dll> -S <sbs>"), edit SBS, and delete models.
 * The "添加模型" button + add panel appear only to users with model.manage. */
(function () {
  "use strict";
  const page = document.querySelector(".lm-models");
  if (!page) return;
  const pid = Number(page.dataset.projectId);
  const rowsEl = document.getElementById("lm-model-rows");
  const addBox = document.getElementById("lm-model-add");
  const toggleBtn = document.getElementById("lm-model-toggle");
  const $ = (id) => document.getElementById(id);
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
    setTimeout(() => { t.hidden = true; }, 3000);
  }
  function showError(msg) {
    const e = $("lm-model-error");
    if (!e) return;
    e.textContent = msg || "";
    e.hidden = !msg;
  }

  const KIND_LABEL = { path: "服务器路径", bundle: "dll + sbs" };
  const CUBE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.3 7 12 12l8.7-5M12 22V12"/></svg>';

  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

  function modelCard(m) {
    const kind = esc(KIND_LABEL[m.kind] || m.kind || "");
    const status = m.exists === false
      ? '<span class="tag tag-bad">服务器缺失</span>'
      : '<span class="tag tag-ok">正常</span>';
    const cur = !!m.is_current;
    const curBadge = cur ? `<span class="tag tag-cur">${CHECK}当前模型</span>` : "";
    // An unversioned model is called out rather than left blank: its results
    // land in the dashboard's "(未标注)" bucket, which is easy to miss until the
    // per-version comparison looks wrong.
    const verBadge = m.version
      ? `<span class="tag tag-ver" title="模型版本">${esc(m.version)}</span>`
      : '<span class="tag tag-nover" title="未标注版本，结果会归入“(未标注)”">未标版本</span>';
    const note = m.version_note
      ? `<div class="mnote" title="版本说明">${esc(m.version_note)}</div>` : "";
    let actions = "";
    if (canManage && !cur) {
      actions += `<button class="btn btn-sm btn-primary lm-model-cur" data-name="${esc(m.name)}">设为当前</button>`;
    }
    if (canManage) {
      actions += `<button class="btn btn-sm lm-model-ver" data-name="${esc(m.name)}">${
        m.version ? "改版本" : "设版本"}</button>`;
      if (m.kind === "bundle") {
        actions += `<button class="btn btn-sm lm-model-sbs" data-name="${esc(m.name)}">编辑 SBS</button>`;
      }
      actions += `<button class="btn btn-sm btn-danger lm-model-del" data-name="${esc(m.name)}">删除</button>`;
    }
    // Managers can click anywhere on a (non-current) card to make it current;
    // the inner action buttons stop propagation so they keep their own intent.
    const selectable = canManage && !cur;
    return `
      <div class="mcard${cur ? " current" : ""}${selectable ? " selectable" : ""}" data-name="${esc(m.name)}"
           ${selectable ? 'role="button" tabindex="0" title="设为当前模型"' : ""}>
        <div class="top">
          <span class="mico">${CUBE}</span>
          <div class="who"><b>${esc(m.name)}</b><span>${kind}</span></div>
          <span style="flex:1"></span>
          ${verBadge}${curBadge}${status}
        </div>
        <div class="mpath">${esc(m.path || "—")}</div>
        ${note}
        <div class="foot">
          <span class="tag tag-kind">${kind}</span>
          <span class="mact">${actions}</span>
        </div>
      </div>`;
  }

  function render(models) {
    if (!models.length) {
      rowsEl.innerHTML = '<div class="state" style="grid-column:1/-1">' +
        `<div class="glyph">${CUBE}</div><h3>尚无模型</h3>` +
        '<p>登记服务器上的 .sil 路径，或上传 dll + sbs + pdb 生成模型。</p></div>';
      return;
    }
    rowsEl.innerHTML = models.map(modelCard).join("");
    rowsEl.querySelectorAll(".lm-model-del").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); removeModel(b.dataset.name); }));
    rowsEl.querySelectorAll(".lm-model-sbs").forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (window.LMSbsModal) { window.LMSbsModal.open(pid, b.dataset.name); }
      }));
    rowsEl.querySelectorAll(".lm-model-ver").forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        openVersion(models.find((m) => m.name === b.dataset.name));
      }));
    rowsEl.querySelectorAll(".lm-model-cur").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); setCurrent(b.dataset.name); }));
    // Whole-card selection for managers (buttons above stop propagation).
    rowsEl.querySelectorAll(".mcard.selectable").forEach((c) => {
      c.addEventListener("click", () => setCurrent(c.dataset.name));
      c.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setCurrent(c.dataset.name); }
      });
    });
  }

  async function setCurrent(name) {
    try {
      const data = await LMApi.setCurrentProjectModel(pid, name);
      render(data.models || []);
      toast(`已将「${name}」设为当前模型`, true);
    } catch (ex) {
      toast(ex.message, false);
    }
  }

  async function load() {
    try {
      const data = await LMApi.listProjectModels(pid);
      canManage = !!data.can_manage;
      if (toggleBtn) { toggleBtn.hidden = !canManage; }
      if (!canManage && addBox) { addBox.hidden = true; }
      render(data.models || []);
    } catch (ex) {
      rowsEl.innerHTML = '<div class="state err" style="grid-column:1/-1">' +
        `<h3>加载失败</h3><p>${esc(ex.message)}</p></div>`;
    }
  }

  async function addPath() {
    showError("");
    const name = $("lm-model-name").value.trim();
    const path = $("lm-model-path").value.trim();
    const verEl = $("lm-model-version");
    const version = verEl ? verEl.value.trim() : "";
    try {
      const data = await LMApi.addProjectModel(pid, name, path, version);
      $("lm-model-name").value = ""; $("lm-model-path").value = "";
      if (verEl) { verEl.value = ""; }
      render(data.models || []);
      toast("已添加模型", true);
    } catch (ex) {
      showError(ex.message);
    }
  }

  async function addBundle() {
    showError("");
    const name = $("lm-bundle-name").value.trim();
    const dll = $("lm-bundle-dll").files[0];
    const sbs = $("lm-bundle-sbs").files[0];
    const pdb = $("lm-bundle-pdb").files[0];
    if (!dll || !sbs || !pdb) { showError("请同时选择 dll、sbs 与 pdb 文件"); return; }
    const bVerEl = $("lm-bundle-version");
    const fd = new FormData();
    if (name) { fd.append("name", name); }
    if (bVerEl && bVerEl.value.trim()) { fd.append("version", bVerEl.value.trim()); }
    fd.append("dll", dll, dll.name);
    fd.append("sbs", sbs, sbs.name);
    fd.append("pdb", pdb, pdb.name);
    const btn = $("lm-bundle-add");
    btn.disabled = true;
    try {
      const data = await LMApi.uploadProjectModel(pid, fd);
      $("lm-bundle-name").value = "";
      $("lm-bundle-dll").value = ""; $("lm-bundle-sbs").value = ""; $("lm-bundle-pdb").value = "";
      if (bVerEl) { bVerEl.value = ""; }
      render(data.models || []);
      toast("已上传并生成 .sil", true);
    } catch (ex) {
      showError(ex.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function removeModel(name) {
    if (!(await LMUI.confirm({
      level: "danger",
      title: `删除模型「${name}」`,
      body: "该模型文件将从项目中移除，引用它的任务将无法运行。",
      confirmText: "删除",
    }))) { return; }
    try {
      const data = await LMApi.removeProjectModel(pid, name);
      render(data.models || []);
      toast("已删除", true);
    } catch (ex) {
      toast(ex.message, false);
    }
  }


  /* ----------------------------------------------------------------------- *
   * Version relabelling modal.
   *
   * The label is validated server-side against LM_MODEL_VERSION_PATTERN, so the
   * error is surfaced inline in the dialog and the dialog STAYS OPEN — the user
   * would otherwise lose the release note they just typed to a rejected token.
   * ----------------------------------------------------------------------- */
  const verModal = $("lm-ver-modal");
  let verTarget = null;

  function verError(msg) {
    const box = $("lm-ver-err");
    if (!box) { return; }
    box.textContent = msg || "";
    box.hidden = !msg;
  }

  function closeVersion() {
    verTarget = null;
    if (verModal) { verModal.hidden = true; }
    verError("");
  }

  function openVersion(model) {
    if (!model || !verModal) { return; }
    verTarget = model.name;
    $("lm-ver-title").textContent = ` — ${model.name}`;
    $("lm-ver-input").value = model.version || "";
    $("lm-ver-note").value = model.version_note || "";
    verError("");
    verModal.hidden = false;
    const input = $("lm-ver-input");
    input.focus();
    input.select();
  }

  async function saveVersion() {
    if (!verTarget) { return; }
    const btn = $("lm-ver-save");
    const version = $("lm-ver-input").value.trim();
    const note = $("lm-ver-note").value.trim();
    verError("");
    btn.disabled = true;
    try {
      const data = await LMApi.updateProjectModelVersion(pid, verTarget, version, note);
      const name = verTarget;
      closeVersion();
      render(data.models || []);
      toast(version ? `「${name}」版本已设为 ${version}` : `已清除「${name}」的版本号`, true);
    } catch (ex) {
      verError(ex.message);
    } finally {
      btn.disabled = false;
    }
  }

  if (verModal) {
    verModal.querySelectorAll("[data-ver-close]").forEach((el) =>
      el.addEventListener("click", closeVersion));
    $("lm-ver-save").addEventListener("click", saveVersion);
    // Enter commits from the single-line label field; the note is multi-line so
    // it keeps Enter for newlines.
    $("lm-ver-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); saveVersion(); }
    });
    verModal.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); closeVersion(); }
    });
  }

  const addBtn = $("lm-model-add-btn");
  if (addBtn) { addBtn.addEventListener("click", addPath); }
  const bundleBtn = $("lm-bundle-add");
  if (bundleBtn) { bundleBtn.addEventListener("click", addBundle); }
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      addBox.hidden = !addBox.hidden;
      if (!addBox.hidden) { const n = $("lm-model-name"); if (n) n.focus(); }
    });
  }

  load();
})();
