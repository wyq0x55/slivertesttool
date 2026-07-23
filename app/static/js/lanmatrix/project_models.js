/* Per-project model management: list the project's .sil models, register a
 * server-side .sil path, or upload a dll + sbs pair (the server generates an
 * empty .sil whose only module line is "<dll> -S <sbs>"), and delete models.
 * Management controls are shown only to users with model.manage (can_manage). */
(function () {
  "use strict";
  const page = document.querySelector(".lm-page");
  const pid = Number(page.dataset.projectId);
  const rows = document.getElementById("lm-model-rows");
  const $ = (id) => document.getElementById(id);
  let canManage = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    if (!t) { return; }
    t.textContent = msg; t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false; setTimeout(() => { t.hidden = true; }, 3000);
  }
  function showError(msg) {
    const e = $("lm-model-error");
    if (!e) { return; }
    e.textContent = msg || ""; e.hidden = !msg;
  }

  const KIND_LABEL = { path: "路径", bundle: "dll+sbs" };

  function render(models) {
    if (!models.length) {
      rows.innerHTML = '<tr><td colspan="5" class="lm-muted">尚无模型</td></tr>';
      return;
    }
    rows.innerHTML = models.map((m) => `
      <tr>
        <td>${esc(m.name)}</td>
        <td>${esc(KIND_LABEL[m.kind] || m.kind || "")}</td>
        <td class="lm-mono">${esc(m.path || "")}</td>
        <td>${m.exists === false
          ? '<span class="lm-err">服务器缺失</span>' : "正常"}</td>
        <td>${canManage
          ? `<button class="lm-btn lm-btn-sm lm-btn-danger lm-model-del" data-name="${esc(m.name)}">删除</button>`
          : ""}</td>
      </tr>`).join("");
    rows.querySelectorAll(".lm-model-del").forEach((b) =>
      b.addEventListener("click", () => removeModel(b.dataset.name)));
  }

  async function load() {
    try {
      const data = await LMApi.listProjectModels(pid);
      canManage = !!data.can_manage;
      const forms = $("lm-model-forms");
      if (forms) { forms.hidden = !canManage; }
      render(data.models || []);
    } catch (ex) {
      rows.innerHTML = `<tr><td colspan="5" class="lm-err">${esc(ex.message)}</td></tr>`;
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
    if (!dll || !sbs) { showError("请同时选择 dll 与 sbs 文件"); return; }
    const fd = new FormData();
    if (name) { fd.append("name", name); }
    fd.append("dll", dll, dll.name);
    fd.append("sbs", sbs, sbs.name);
    const btn = $("lm-bundle-add");
    btn.disabled = true;
    try {
      const data = await LMApi.uploadProjectModel(pid, fd);
      $("lm-bundle-name").value = "";
      $("lm-bundle-dll").value = ""; $("lm-bundle-sbs").value = "";
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

  const addBtn = $("lm-model-add");
  if (addBtn) { addBtn.addEventListener("click", addPath); }
  const bundleBtn = $("lm-bundle-add");
  if (bundleBtn) { bundleBtn.addEventListener("click", addBundle); }

  load();
})();
