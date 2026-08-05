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

  function modelCard(m) {
    const kind = esc(KIND_LABEL[m.kind] || m.kind || "");
    const status = m.exists === false
      ? '<span class="tag tag-bad">服务器缺失</span>'
      : '<span class="tag tag-ok">正常</span>';
    let actions = "";
    if (canManage) {
      if (m.kind === "bundle") {
        actions += `<button class="btn btn-sm lm-model-sbs" data-name="${esc(m.name)}">编辑 SBS</button>`;
      }
      actions += `<button class="btn btn-sm btn-danger lm-model-del" data-name="${esc(m.name)}">删除</button>`;
    }
    return `
      <div class="mcard" data-name="${esc(m.name)}">
        <div class="top">
          <span class="mico">${CUBE}</span>
          <div class="who"><b>${esc(m.name)}</b><span>${kind}</span></div>
          <span style="flex:1"></span>
          ${status}
        </div>
        <div class="mpath">${esc(m.path || "—")}</div>
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
      b.addEventListener("click", () => removeModel(b.dataset.name)));
    rowsEl.querySelectorAll(".lm-model-sbs").forEach((b) =>
      b.addEventListener("click", () => {
        if (window.LMSbsModal) { window.LMSbsModal.open(pid, b.dataset.name); }
      }));
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
    try {
      const data = await LMApi.addProjectModel(pid, name, path);
      $("lm-model-name").value = ""; $("lm-model-path").value = "";
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
    const fd = new FormData();
    if (name) { fd.append("name", name); }
    fd.append("dll", dll, dll.name);
    fd.append("sbs", sbs, sbs.name);
    fd.append("pdb", pdb, pdb.name);
    const btn = $("lm-bundle-add");
    btn.disabled = true;
    try {
      const data = await LMApi.uploadProjectModel(pid, fd);
      $("lm-bundle-name").value = "";
      $("lm-bundle-dll").value = ""; $("lm-bundle-sbs").value = ""; $("lm-bundle-pdb").value = "";
      render(data.models || []);
      toast("已上传并生成 .sil", true);
    } catch (ex) {
      showError(ex.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function removeModel(name) {
    if (!confirm(`确认删除模型 "${name}"？`)) { return; }
    try {
      const data = await LMApi.removeProjectModel(pid, name);
      render(data.models || []);
      toast("已删除", true);
    } catch (ex) {
      toast(ex.message, false);
    }
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
