(function() {
  "use strict";
  const MAX_PROCEDURE_ROWS = 200;
  (function initQualificationModule(win, doc) {
    let cleanRoomState = false;
    let cleanRoomLoaded = false;
    let cachedSignalList = "";
    function resolveApi(explicit) {
      if (explicit) return explicit;
      if (win && win.LMApi) return win.LMApi;
      if (typeof LMApi !== "undefined") return LMApi;
      return null;
    }
    function resolveUi() {
      if (win && win.LMUI) return win.LMUI;
      if (typeof LMUI !== "undefined") return LMUI;
      return null;
    }
    function resolveProjectId() {
      if (doc && typeof doc.querySelector === "function") {
        const scoped = doc.querySelector(".lm-exec-config, .lm-editor");
        if (scoped && scoped.dataset && scoped.dataset.projectId) {
          const parsed = Number(scoped.dataset.projectId);
          if (parsed > 0) return parsed;
        }
      }
      if (win && win.LM && win.LM.projectId) {
        const parsed = Number(win.LM.projectId);
        if (parsed > 0) return parsed;
      }
      return 0;
    }
    function byId(id) {
      if (!doc || typeof doc.getElementById !== "function") return null;
      return doc.getElementById(id);
    }
    function getToggleInput() {
      return byId("lm-clean-room-toggle");
    }
    function isCleanRoomEnabled() {
      const toggle = getToggleInput();
      if (toggle && typeof toggle.checked === "boolean") {
        cleanRoomState = Boolean(toggle.checked);
        return cleanRoomState;
      }
      return cleanRoomState;
    }
    function setCleanRoomEnabled(enabled) {
      cleanRoomState = Boolean(enabled);
      const toggle = getToggleInput();
      if (toggle) {
        toggle.checked = cleanRoomState;
      }
    }
    const CLEAN_ROOM_UNREAD = "无法读取车规洁净室设置，已取消入队";
    async function loadCleanRoomSetting() {
      const toggle = getToggleInput();
      if (toggle) {
        cleanRoomLoaded = true;
        return isCleanRoomEnabled();
      }
      if (cleanRoomLoaded) return cleanRoomState;
      const api = resolveApi();
      const pid = resolveProjectId();
      if (!api || typeof api.getProject !== "function" || !(pid > 0)) {
        throw new Error(CLEAN_ROOM_UNREAD);
      }
      let data;
      try {
        data = await api.getProject(pid);
      } catch (_e) {
        throw new Error(CLEAN_ROOM_UNREAD);
      }
      const proj = data && data.project || data || {};
      if (typeof proj.default_clean_room !== "boolean") {
        throw new Error(CLEAN_ROOM_UNREAD);
      }
      cleanRoomState = proj.default_clean_room;
      cleanRoomLoaded = true;
      return cleanRoomState;
    }
    function setStatus(id, message) {
      const el = byId(id);
      if (el) el.textContent = message;
    }
    function toast(message, ok) {
      const ui = resolveUi();
      if (ui && typeof ui.toast === "function") ui.toast(message, ok);
    }
    async function persistCleanRoom(enabled) {
      const previous = cleanRoomState;
      const api = resolveApi();
      const pid = resolveProjectId();
      setCleanRoomEnabled(enabled);
      const fail = (message) => {
        setCleanRoomEnabled(previous);
        setStatus("lm-clean-room-status", message);
        toast(message, false);
      };
      if (!api || typeof api.patchProject !== "function" || pid <= 0) {
        fail("保存失败");
        return;
      }
      try {
        await api.patchProject(pid, { default_clean_room: cleanRoomState });
        cleanRoomLoaded = true;
        setStatus("lm-clean-room-status", cleanRoomState ? "已开启车规洁净室" : "已关闭车规洁净室");
        toast("执行配置已保存", true);
      } catch (err) {
        fail(err && err.message || "保存失败");
      }
    }
    function ensureSignalListDialog() {
      const existing = byId("lm-signal-list-dialog");
      if (existing || !doc || typeof doc.createElement !== "function") return existing;
      const dialog = doc.createElement("dialog");
      dialog.id = "lm-signal-list-dialog";
      dialog.className = "lm-dialog";
      if (typeof dialog.setAttribute === "function") {
        dialog.setAttribute("aria-labelledby", "lm-signal-list-title");
      }
      dialog.innerHTML = `
      <form method="dialog" class="lm-card">
        <h3 id="lm-signal-list-title">默认信号集配置 (default_signal_list)</h3>
        <p class="lm-muted" id="lm-signal-list-help">
          当测试用例与模型目录都没有 output.txt 时，用这里的观测信号列表兜底，
          避免整变量 1ms CSV 写入把磁盘写爆。每行填写一个观测信号名。
        </p>
        <label class="lm-muted" for="lm-default-signal-list">观测信号列表</label>
        <textarea id="lm-default-signal-list" rows="8" spellcheck="false"
          aria-describedby="lm-signal-list-help"
          placeholder="例如：&#10;v_speed&#10;u1_brake_lamp"></textarea>
        <p class="lm-muted" id="lm-signal-list-status" role="status" aria-live="polite"></p>
        <menu class="lm-dialog-actions">
          <button class="btn" id="lm-signal-list-cancel" type="button">取消</button>
          <button class="btn btn-primary" id="lm-signal-list-save" type="button">保存默认信号集</button>
        </menu>
      </form>`;
      doc.body.appendChild(dialog);
      const cancelBtn = byId("lm-signal-list-cancel");
      if (cancelBtn && typeof cancelBtn.addEventListener === "function") {
        cancelBtn.addEventListener("click", () => {
          if (typeof dialog.close === "function") dialog.close();
          else dialog.hidden = true;
        });
      }
      const saveBtn = byId("lm-signal-list-save");
      if (saveBtn && typeof saveBtn.addEventListener === "function") {
        saveBtn.addEventListener("click", async () => {
          const input = byId("lm-default-signal-list");
          const text = input ? String(input.value || "") : "";
          await saveDefaultSignalList(text);
        });
      }
      return dialog;
    }
    async function openSignalListModal() {
      const dialog = ensureSignalListDialog();
      const api = resolveApi();
      const pid = resolveProjectId();
      const textarea = byId("lm-default-signal-list");
      setStatus("lm-signal-list-status", "");
      if (api && typeof api.getProject === "function" && pid > 0) {
        try {
          const data = await api.getProject(pid);
          const proj = data && data.project || data || {};
          cachedSignalList = String(proj.default_signal_list || "");
        } catch (_e) {
        }
      }
      if (textarea) textarea.value = cachedSignalList;
      if (dialog) {
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.hidden = false;
      }
    }
    async function saveDefaultSignalList(signalListText) {
      const api = resolveApi();
      const pid = resolveProjectId();
      if (!api || typeof api.patchProject !== "function" || pid <= 0) return null;
      const normalized = String(signalListText || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n").map((line) => line.trim()).filter(Boolean).join("\n");
      try {
        const res = await api.patchProject(pid, { default_signal_list: normalized });
        cachedSignalList = normalized;
        setStatus("lm-signal-list-status", "已保存默认信号集");
        toast("默认信号集已保存", true);
        const dialog = byId("lm-signal-list-dialog");
        if (dialog) {
          if (typeof dialog.close === "function") dialog.close();
          else dialog.hidden = true;
        }
        return res;
      } catch (err) {
        const msg = err && err.message || "保存失败";
        setStatus("lm-signal-list-status", msg);
        toast(msg, false);
        throw err;
      }
    }
    function parseRowIds(raw) {
      const seen = [];
      String(raw || "").split(/[^0-9]+/).forEach((part) => {
        if (!part) return;
        const id = Number(part);
        if (!Number.isInteger(id) || id <= 0) return;
        if (seen.indexOf(id) >= 0) return;
        seen.push(id);
      });
      return seen;
    }
    async function generateProcedureDraft() {
      const api = resolveApi();
      const pid = resolveProjectId();
      const rowInput = byId("lm-ai-row-ids");
      const modelInput = byId("lm-ai-model-id");
      const button = byId("lm-ai-generate");
      const link = byId("lm-ai-draft-link");
      const ids = parseRowIds(rowInput ? rowInput.value : "");
      if (!ids.length) {
        setStatus("lm-ai-status", "请输入已保存的测试行 ID（可用逗号或换行分隔）。");
        return;
      }
      if (ids.length > MAX_PROCEDURE_ROWS) {
        setStatus("lm-ai-status", `一次最多生成 ${MAX_PROCEDURE_ROWS} 条测试行的手顺草稿。`);
        return;
      }
      const modelRaw = modelInput ? String(modelInput.value || "").trim() : "";
      let modelId = 0;
      if (modelRaw) {
        modelId = Number(modelRaw);
        if (!Number.isInteger(modelId) || modelId <= 0) {
          setStatus("lm-ai-status", "已保存模型 ID 必须为正整数。");
          return;
        }
      }
      if (!api || typeof api.createAiDraft !== "function" || pid <= 0) {
        setStatus("lm-ai-status", "缺少项目上下文，无法生成草稿。");
        return;
      }
      if (button) button.disabled = true;
      setStatus("lm-ai-status", "正在生成手顺草稿…");
      try {
        const payload = { item_ids: ids };
        if (modelId) payload.model_id = modelId;
        const draft = await api.createAiDraft("procedure", pid, payload);
        if (draft && draft.error) {
          setStatus("lm-ai-status", String(draft.error));
          return;
        }
        const status = draft && draft.status ? `（草稿状态：${draft.status}）` : "";
        setStatus("lm-ai-status", `已创建手顺草稿；请人工审核后落库。${status}`);
        if (link && draft && draft.id != null) {
          link.href = `/lanmatrix/projects/${pid}/ai?draft=${encodeURIComponent(draft.id)}`;
          link.textContent = `查看草稿 #${draft.id}`;
          link.hidden = false;
        }
      } catch (err) {
        setStatus("lm-ai-status", err && err.message || "生成失败");
      } finally {
        if (button) button.disabled = false;
      }
    }
    function isSettingsPage() {
      if (!doc || typeof doc.querySelector !== "function") return false;
      return Boolean(doc.querySelector(".lm-exec-config"));
    }
    async function initSettingsPage() {
      const api = resolveApi();
      const pid = resolveProjectId();
      const toggle = getToggleInput();
      if (toggle && typeof toggle.addEventListener === "function") {
        toggle.addEventListener("change", () => {
          void persistCleanRoom(Boolean(toggle.checked));
        });
      }
      if (toggle && api && typeof api.getProject === "function" && pid > 0) {
        try {
          const data = await api.getProject(pid);
          const proj = data && data.project || data || {};
          cleanRoomLoaded = true;
          setCleanRoomEnabled(proj.default_clean_room === true);
          setStatus(
            "lm-clean-room-status",
            cleanRoomState ? "当前已开启：每次入队都会冷启动全新的 Silver 实例" : "当前已关闭"
          );
        } catch (err) {
          setStatus("lm-clean-room-status", err && err.message || "读取执行配置失败");
        }
      }
      const sigBtn = byId("lm-signal-list-btn");
      if (sigBtn && typeof sigBtn.addEventListener === "function") {
        sigBtn.addEventListener("click", () => {
          void openSignalListModal();
        });
      }
      const aiBtn = byId("lm-ai-generate");
      if (aiBtn && typeof aiBtn.addEventListener === "function") {
        aiBtn.addEventListener("click", () => {
          void generateProcedureDraft();
        });
      }
    }
    function wrapApi(targetApi) {
      const api = resolveApi(targetApi);
      if (!api) return;
      if (typeof api.runSelectedTasks === "function" && !api.runSelectedTasks.__lmQualWrapped) {
        const origRun = api.runSelectedTasks;
        const wrappedRun = async function(id, payload) {
          const cleanRoom = await loadCleanRoomSetting();
          const nextPayload = Object.assign({}, payload || {});
          if (cleanRoom) {
            nextPayload.clean_room = true;
          } else {
            delete nextPayload.clean_room;
          }
          return origRun.call(this, id, nextPayload);
        };
        wrappedRun.__lmQualWrapped = true;
        api.runSelectedTasks = wrappedRun;
      }
      if (typeof api.rerunSelectedTasks === "function" && !api.rerunSelectedTasks.__lmQualWrapped) {
        const origRerun = api.rerunSelectedTasks;
        const wrappedRerun = async function(id, keysOrPayload, opts) {
          const cleanRoom = await loadCleanRoomSetting();
          if (keysOrPayload && typeof keysOrPayload === "object" && !Array.isArray(keysOrPayload)) {
            const nextObj = Object.assign({}, keysOrPayload);
            if (cleanRoom) {
              nextObj.clean_room = true;
            } else {
              delete nextObj.clean_room;
            }
            return origRerun.call(this, id, nextObj);
          }
          if (cleanRoom) {
            const nextOpts = Object.assign({}, opts || {}, { clean_room: true });
            return origRerun.call(this, id, keysOrPayload, nextOpts);
          }
          if (opts !== void 0) {
            const nextOpts = Object.assign({}, opts);
            delete nextOpts.clean_room;
            return origRerun.call(this, id, keysOrPayload, nextOpts);
          }
          return origRerun.call(this, id, keysOrPayload);
        };
        wrappedRerun.__lmQualWrapped = true;
        api.rerunSelectedTasks = wrappedRerun;
      }
    }
    function init() {
      if (isSettingsPage()) void initSettingsPage();
      wrapApi();
    }
    const controller = {
      init,
      isCleanRoomEnabled,
      setCleanRoomEnabled,
      loadCleanRoomSetting,
      wrapApi,
      openSignalListModal,
      saveDefaultSignalList
    };
    if (win) {
      win.LMEditorQualification = controller;
    }
    globalThis.LMEditorQualification = controller;
    if (doc && doc.readyState === "loading" && typeof doc.addEventListener === "function") {
      doc.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  })(typeof window !== "undefined" ? window : globalThis, typeof document !== "undefined" ? document : null);
})();
