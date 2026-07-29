/* In-app SBS editor controller (build-free).
 *
 * Drives the #lm-sbs-modal defined in models.html:
 *  - loads a bundle model's .sbs via LMApi.getModelSbs;
 *  - mounts CodeMirror 6 (window.LMSbsEditor from vendor/sbs/sbs-editor.umd.js,
 *    which highlights using the user's own VS Code TextMate grammar) or falls
 *    back to a plain <textarea> when the bundle is absent;
 *  - saves with an optimistic lock (base_version); on 409 SBS_CONFLICT offers
 *    to load the server version or force-overwrite;
 *  - browses / loads / restores the last 50 revisions.
 *
 * Exposes window.LMSbsModal = { open(projectId, modelName) }.
 */
(function () {
  "use strict";
  const global = window;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function fmtSize(n) {
    n = Number(n) || 0;
    return n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KB";
  }
  function fmtTime(iso) {
    if (!iso) { return ""; }
    try {
      const d = new Date(iso);
      const p = (x) => String(x).padStart(2, "0");
      return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
        " " + p(d.getHours()) + ":" + p(d.getMinutes());
    } catch (e) { return iso; }
  }

  const assets = global.LM_SBS_ASSETS || {};

  // Editor abstraction: CodeMirror 6 bundle if present, else a <textarea>.
  async function mountEditor(container, doc, onChange) {
    if (global.LMSbsEditor && assets.wasm && assets.grammar) {
      try {
        return await global.LMSbsEditor.mount({
          container: container,
          doc: doc,
          wasmUrl: assets.wasm,
          grammarUrl: assets.grammar,
          onChange: onChange,
        });
      } catch (ex) {
        // Fall through to textarea on any bundle/wasm failure.
        console.warn("SBS highlight bundle failed, using textarea:", ex);
      }
    }
    const ta = document.createElement("textarea");
    ta.className = "lm-sbs-textarea";
    ta.value = doc || "";
    ta.spellcheck = false;
    ta.addEventListener("input", () => onChange && onChange(ta.value));
    container.appendChild(ta);
    return {
      getValue: () => ta.value,
      setValue: (t) => { ta.value = t || ""; },
      setReadOnly: (ro) => { ta.readOnly = !!ro; },
      focus: () => ta.focus(),
      destroy: () => { ta.remove(); },
    };
  }

  const state = {
    pid: null, name: null, editor: null, baseVersion: "",
    filename: "", dirty: false, historyOpen: false,
  };

  function setStatus(msg, kind) {
    const el = $("lm-sbs-status");
    if (!el) { return; }
    el.textContent = msg || "";
    el.className = "lm-sbs-status" + (kind ? " lm-sbs-" + kind : "");
  }

  function markDirty(dirty) {
    state.dirty = dirty;
    const save = $("lm-sbs-save");
    if (save) { save.disabled = !dirty; }
    if (dirty) { setStatus("有未保存的修改", "warn"); }
  }

  async function open(projectId, modelName) {
    state.pid = projectId;
    state.name = modelName;
    state.dirty = false;
    state.historyOpen = false;
    const modal = $("lm-sbs-modal");
    const host = $("lm-sbs-editor");
    const hist = $("lm-sbs-history");
    if (!modal || !host) { return; }
    host.innerHTML = "";
    if (hist) { hist.hidden = true; }
    $("lm-sbs-title").textContent = "：" + modelName;
    modal.hidden = false;
    setStatus("载入中…");
    if ($("lm-sbs-save")) { $("lm-sbs-save").disabled = true; }

    let data;
    try {
      data = await LMApi.getModelSbs(projectId, modelName);
    } catch (ex) {
      setStatus("载入失败：" + ex.message, "err");
      return;
    }
    const sbs = data.sbs || {};
    state.baseVersion = sbs.version || "";
    state.filename = sbs.filename || "";
    $("lm-sbs-title").textContent = "：" + modelName +
      (sbs.filename ? "  (" + sbs.filename + ")" : "");

    if (state.editor) { try { state.editor.destroy(); } catch (e) { /* ignore */ } }
    state.editor = await mountEditor(host, sbs.content || "", () => markDirty(true));
    markDirty(false);
    setStatus("已载入 · " + fmtSize(sbs.size) + " · 版本 " +
      (state.baseVersion || "").slice(0, 8), "ok");
    state.editor.focus();
  }

  function close() {
    if (state.dirty && !confirm("有未保存的修改，确定关闭？")) { return; }
    const modal = $("lm-sbs-modal");
    if (modal) { modal.hidden = true; }
    if (state.editor) { try { state.editor.destroy(); } catch (e) { /* ignore */ } }
    state.editor = null;
  }

  function showConflict(server) {
    // server = { filename, content, version, size } (the current on-disk state)
    setStatus("保存失败：该文件已被他人修改", "err");
    const bar = $("lm-sbs-conflict");
    if (!bar) { return; }
    bar.hidden = false;
    bar.innerHTML =
      '<span>该 SBS 已被其他人修改（服务器版本 ' +
      esc((server.version || "").slice(0, 8)) + '）。</span>' +
      '<button class="lm-btn lm-btn-sm" id="lm-sbs-loadsrv">载入服务器版本</button>' +
      '<button class="lm-btn lm-btn-sm lm-btn-danger" id="lm-sbs-force">仍然覆盖</button>' +
      '<button class="lm-btn lm-btn-sm" id="lm-sbs-conflict-x">关闭</button>';
    $("lm-sbs-loadsrv").addEventListener("click", () => {
      state.editor.setValue(server.content || "");
      state.baseVersion = server.version || "";
      bar.hidden = true;
      markDirty(false);
      setStatus("已载入服务器版本", "ok");
    });
    $("lm-sbs-force").addEventListener("click", () => {
      // Overwrite: adopt the server version as our base so the next save wins.
      state.baseVersion = server.version || "";
      bar.hidden = true;
      save();
    });
    $("lm-sbs-conflict-x").addEventListener("click", () => { bar.hidden = true; });
  }

  async function save() {
    if (!state.editor) { return; }
    const content = state.editor.getValue();
    const btn = $("lm-sbs-save");
    if (btn) { btn.disabled = true; }
    setStatus("保存中…");
    try {
      const res = await LMApi.saveModelSbs(state.pid, {
        name: state.name, content: content, base_version: state.baseVersion,
      });
      const sbs = res.sbs || {};
      state.baseVersion = sbs.version || state.baseVersion;
      markDirty(false);
      const bar = $("lm-sbs-conflict");
      if (bar) { bar.hidden = true; }
      setStatus(sbs.unchanged ? "无改动" :
        "已保存 · 版本 " + (state.baseVersion || "").slice(0, 8), "ok");
      if (state.historyOpen) { loadHistory(); }
    } catch (ex) {
      if (ex.code === "SBS_CONFLICT" && ex.details) {
        showConflict(ex.details);
      } else {
        setStatus("保存失败：" + ex.message, "err");
      }
      if (btn) { btn.disabled = false; }
    }
  }

  async function loadHistory() {
    const list = $("lm-sbs-rev-list");
    if (!list) { return; }
    list.innerHTML = '<li class="lm-muted">加载中…</li>';
    let data;
    try {
      data = await LMApi.listModelSbsRevisions(state.pid, state.name);
    } catch (ex) {
      list.innerHTML = '<li class="lm-err">' + esc(ex.message) + "</li>";
      return;
    }
    const revs = data.revisions || [];
    if (!revs.length) {
      list.innerHTML = '<li class="lm-muted">暂无历史版本</li>';
      return;
    }
    list.innerHTML = revs.map((r) =>
      '<li class="lm-sbs-rev">' +
      '<div class="lm-sbs-rev-meta">' +
      '<span class="lm-sbs-rev-time">' + esc(fmtTime(r.created_at)) + "</span>" +
      (r.is_current ? '<span class="lm-sbs-rev-cur">当前</span>' : "") +
      '<span class="lm-sbs-rev-size">' + esc(fmtSize(r.size)) + "</span>" +
      "</div>" +
      '<div class="lm-sbs-rev-act">' +
      '<button class="lm-btn lm-btn-sm lm-sbs-rev-load" data-id="' + r.id + '">载入</button>' +
      '<button class="lm-btn lm-btn-sm lm-sbs-rev-restore" data-id="' + r.id + '">恢复</button>' +
      "</div></li>").join("");
    list.querySelectorAll(".lm-sbs-rev-load").forEach((b) =>
      b.addEventListener("click", () => loadRevision(Number(b.dataset.id))));
    list.querySelectorAll(".lm-sbs-rev-restore").forEach((b) =>
      b.addEventListener("click", () => restoreRevision(Number(b.dataset.id))));
  }

  async function loadRevision(revId) {
    try {
      const data = await LMApi.getModelSbsRevision(state.pid, state.name, revId);
      const rev = data.revision || {};
      state.editor.setValue(rev.content || "");
      markDirty(true);
      setStatus("已把历史版本 #" + revId + " 载入编辑器，保存后生效", "warn");
    } catch (ex) {
      setStatus("载入历史版本失败：" + ex.message, "err");
    }
  }

  async function restoreRevision(revId) {
    if (!confirm("确认把 SBS 恢复到历史版本 #" + revId + "？当前内容会先备份并记入历史。")) {
      return;
    }
    try {
      const res = await LMApi.restoreModelSbsRevision(state.pid, state.name, revId);
      const sbs = res.sbs || {};
      state.baseVersion = sbs.version || state.baseVersion;
      // Reflect restored content in the editor.
      const rev = await LMApi.getModelSbsRevision(state.pid, state.name, revId);
      state.editor.setValue((rev.revision || {}).content || "");
      markDirty(false);
      setStatus("已恢复到版本 #" + revId, "ok");
      loadHistory();
    } catch (ex) {
      if (ex.code === "SBS_CONFLICT" && ex.details) {
        showConflict(ex.details);
      } else {
        setStatus("恢复失败：" + ex.message, "err");
      }
    }
  }

  function toggleHistory() {
    const hist = $("lm-sbs-history");
    if (!hist) { return; }
    state.historyOpen = hist.hidden;
    hist.hidden = !hist.hidden;
    if (state.historyOpen) { loadHistory(); }
  }

  // Wire static controls once the DOM is ready.
  function init() {
    const saveBtn = $("lm-sbs-save");
    if (saveBtn) { saveBtn.addEventListener("click", () => save()); }
    const reload = $("lm-sbs-reload");
    if (reload) {
      reload.addEventListener("click", () => {
        if (state.dirty && !confirm("重新载入将丢弃未保存的修改，继续？")) { return; }
        open(state.pid, state.name);
      });
    }
    const histBtn = $("lm-sbs-history-toggle");
    if (histBtn) { histBtn.addEventListener("click", toggleHistory); }
    document.querySelectorAll("[data-sbs-close]").forEach((el) =>
      el.addEventListener("click", close));
    document.addEventListener("keydown", (e) => {
      const modal = $("lm-sbs-modal");
      if (!modal || modal.hidden) { return; }
      if (e.key === "Escape") { close(); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (!state.dirty) { return; }
        save();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  global.LMSbsModal = { open: open };
})();
