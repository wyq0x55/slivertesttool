/* Projects list: create blank, import-as-new, open editor. */
(function () {
  "use strict";
  const rowsEl = document.getElementById("lm-project-rows");
  const dialog = document.getElementById("lm-new-dialog");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3000);
  }

  async function load() {
    try {
      const data = await LMApi.listProjects();
      const projects = data.projects || [];
      if (!projects.length) {
        rowsEl.innerHTML = '<tr><td colspan="6" class="lm-muted">还没有项目，点右上角新建。</td></tr>';
        return;
      }
      rowsEl.innerHTML = projects.map((p) => `
        <tr>
          <td><code>${esc(p.code)}</code></td>
          <td><a href="/lanmatrix/projects/${p.id}">${esc(p.name)}</a></td>
          <td><span class="lm-badge lm-status-${esc(p.status)}">${esc(p.status)}</span></td>
          <td>${p.member_count}</td>
          <td class="lm-muted">${esc((p.updated_at || "").replace("T", " ").replace("Z", ""))}</td>
          <td class="lm-row-actions">
            <a class="lm-btn lm-btn-sm" href="/lanmatrix/projects/${p.id}">打开</a>
            <a class="lm-btn lm-btn-sm" href="/lanmatrix/projects/${p.id}/tasks">任务</a>
            <a class="lm-btn lm-btn-sm" href="/lanmatrix/projects/${p.id}/members">成员</a>
            <button class="lm-btn lm-btn-sm lm-btn-danger" data-del="${p.id}"
              data-code="${esc(p.code)}" data-name="${esc(p.name)}">删除</button>
          </td>
        </tr>`).join("");
      rowsEl.querySelectorAll("button[data-del]").forEach((btn) => {
        btn.addEventListener("click", () => onDelete(
          btn.getAttribute("data-del"),
          btn.getAttribute("data-code"),
          btn.getAttribute("data-name")));
      });
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      rowsEl.innerHTML = `<tr><td colspan="6" class="lm-err">${esc(ex.message)}</td></tr>`;
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

  document.getElementById("lm-new-project").addEventListener("click", () => {
    document.getElementById("lm-np-error").hidden = true;
    dialog.showModal();
  });

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

  // Import as new: create blank project from filename, then import into it.
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
