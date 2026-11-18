declare const LMApi: any;
declare const LMUI: any;

export interface RunSelectedPayload {
  test_ids?: string[];
  model?: string;
  expected_inputs?: any;
  clean_room?: boolean;
  [key: string]: any;
}

export interface QualificationProjectInfo {
  id?: number;
  code?: string;
  name?: string;
  default_signal_list?: string;
  is_editable?: boolean;
}

export interface EditorQualificationController {
  init(): void;
  syncVisibility(): void;
  isCleanRoomEnabled(): boolean;
  setCleanRoomEnabled(enabled: boolean): void;
  wrapApi(targetApi?: any): void;
  openSignalListModal(): Promise<void>;
  saveDefaultSignalList(signalListText: string): Promise<any>;
}

const CLEAN_ROOM_TITLE =
  "强制冷启动全新 Silver 实例并在跑完后立即销毁，满足 ASPICE SWE.4-SWE.6 零跨用例污染要求";

(function initQualificationModule(win: any, doc: any) {
  let cleanRoomState = false;
  let cachedSignalList = "";

  function resolveApi(explicit?: any): any {
    if (explicit) return explicit;
    if (win && win.LMApi) return win.LMApi;
    if (typeof LMApi !== "undefined") return LMApi;
    return null;
  }

  function resolveUi(): any {
    if (win && win.LMUI) return win.LMUI;
    if (typeof LMUI !== "undefined") return LMUI;
    return null;
  }

  function resolveProjectId(): number {
    if (doc && typeof doc.querySelector === "function") {
      const editorEl = doc.querySelector(".lm-editor");
      if (editorEl && editorEl.dataset && editorEl.dataset.projectId) {
        const parsed = Number(editorEl.dataset.projectId);
        if (parsed > 0) return parsed;
      }
    }
    if (win && win.LM && win.LM.projectId) {
      const parsed = Number(win.LM.projectId);
      if (parsed > 0) return parsed;
    }
    return 0;
  }

  function getToggleInput(): HTMLInputElement | null {
    if (!doc || typeof doc.getElementById !== "function") return null;
    return doc.getElementById("lm-clean-room-toggle") as HTMLInputElement | null;
  }

  function isCleanRoomEnabled(): boolean {
    const toggle = getToggleInput();
    if (toggle && typeof toggle.checked === "boolean") {
      cleanRoomState = Boolean(toggle.checked);
      return cleanRoomState;
    }
    return cleanRoomState;
  }

  function setCleanRoomEnabled(enabled: boolean): void {
    cleanRoomState = Boolean(enabled);
    const toggle = getToggleInput();
    if (toggle) {
      toggle.checked = cleanRoomState;
    }
  }

  function insertBeforeOrAppend(parent: any, node: any, refNode: any): void {
    if (!parent || !node) return;
    if (refNode && refNode.parentNode === parent && typeof parent.insertBefore === "function") {
      parent.insertBefore(node, refNode);
    } else if (typeof parent.appendChild === "function") {
      parent.appendChild(node);
    }
  }

  function syncVisibility(): void {
    if (!doc || typeof doc.getElementById !== "function") return;
    const runBtn = doc.getElementById("lm-run-selected");
    const sigBtn = doc.getElementById("lm-signal-list-btn");
    const wrap = doc.getElementById("lm-clean-room-wrap");
    const shouldHide = Boolean(runBtn && runBtn.hidden);
    if (sigBtn) sigBtn.hidden = shouldHide;
    if (wrap) wrap.hidden = shouldHide;
  }

  function ensureToolbarControls(): void {
    if (!doc || typeof doc.getElementById !== "function" || typeof doc.createElement !== "function") {
      return;
    }
    const toolbarRight = doc.getElementById("lm-toolbar-right");
    const runBtn = doc.getElementById("lm-run-selected");
    const host = (runBtn && runBtn.parentNode) || doc.getElementById("lm-topbar-exec") || toolbarRight || null;

    if (!doc.getElementById("lm-signal-list-btn") && host) {
      const sigBtn = doc.createElement("button");
      sigBtn.type = "button";
      sigBtn.id = "lm-signal-list-btn";
      sigBtn.className = "btn btn-sm";
      sigBtn.title = "配置项目默认观测信号集 (default_signal_list)";
      sigBtn.textContent = "默认信号集";
      if (typeof sigBtn.addEventListener === "function") {
        sigBtn.addEventListener("click", () => {
          void openSignalListModal();
        });
      }
      insertBeforeOrAppend(host, sigBtn, runBtn);
    }

    let toggle = doc.getElementById("lm-clean-room-toggle");
    if (!toggle && host) {
      const wrap = doc.createElement("label");
      wrap.id = "lm-clean-room-wrap";
      wrap.className = "lm-inline lm-muted";
      wrap.title = CLEAN_ROOM_TITLE;
      if (wrap.style) {
        wrap.style.display = "inline-flex";
        wrap.style.alignItems = "center";
        wrap.style.gap = "4px";
        wrap.style.cursor = "pointer";
      }
      if (runBtn && runBtn.hidden) {
        wrap.hidden = true;
      }

      toggle = doc.createElement("input");
      toggle.type = "checkbox";
      toggle.id = "lm-clean-room-toggle";
      toggle.title = CLEAN_ROOM_TITLE;
      toggle.checked = cleanRoomState;
      if (typeof toggle.addEventListener === "function") {
        toggle.addEventListener("change", () => {
          cleanRoomState = Boolean(toggle.checked);
        });
      }
      if (typeof wrap.appendChild === "function") {
        wrap.appendChild(toggle);
        if (typeof doc.createTextNode === "function") {
          wrap.appendChild(doc.createTextNode(" 车规洁净室"));
        } else {
          wrap.textContent = "车规洁净室";
        }
      }
      insertBeforeOrAppend(host, wrap, runBtn);
    } else if (toggle && typeof toggle.addEventListener === "function" && !(toggle as any).__lmQualBound) {
      (toggle as any).__lmQualBound = true;
      toggle.title = toggle.title || CLEAN_ROOM_TITLE;
      toggle.addEventListener("change", () => {
        cleanRoomState = Boolean(toggle.checked);
      });
    }

    const ObserverCtor = win && win.MutationObserver;
    if (runBtn && typeof ObserverCtor === "function" && !(runBtn as any).__lmQualVisObserved) {
      try {
        const observer = new ObserverCtor(() => {
          syncVisibility();
        });
        observer.observe(runBtn, { attributes: true, attributeFilter: ["hidden"] });
        (runBtn as any).__lmQualVisObserved = true;
      } catch (_e) { /* ignore mock DOM limitations */ }
    }

    syncVisibility();
  }

  function ensureSignalListDialog(): any {
    if (!doc || typeof doc.getElementById !== "function" || typeof doc.createElement !== "function") {
      return null;
    }
    let dialog = doc.getElementById("lm-signal-list-dialog");
    if (dialog) return dialog;
    if (!doc.body || typeof doc.body.appendChild !== "function") return null;

    dialog = doc.createElement("dialog");
    dialog.id = "lm-signal-list-dialog";
    dialog.className = "lm-dialog";

    const form = doc.createElement("form");
    form.method = "dialog";
    form.className = "lm-card";

    const heading = doc.createElement("h3");
    heading.textContent = "默认信号集配置 (default_signal_list)";

    const desc = doc.createElement("p");
    desc.className = "lm-muted";
    desc.textContent =
      "每行填写一个观测信号名。当测试用例与模型目录均未提供 output.txt 时自动落盘，避免全量信号采样导致磁盘膨胀。";

    const textarea = doc.createElement("textarea");
    textarea.id = "lm-default-signal-list";
    textarea.rows = 8;
    if (textarea.style) {
      textarea.style.width = "100%";
      textarea.style.fontFamily = "monospace";
    }
    textarea.placeholder = "例如：\nv_speed\nu1_brake_lamp";

    const status = doc.createElement("p");
    status.id = "lm-signal-list-status";
    status.className = "lm-muted";
    if (typeof status.setAttribute === "function") {
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
    }

    const menu = doc.createElement("menu");

    const cancelBtnEl = doc.createElement("button");
    cancelBtnEl.type = "button";
    cancelBtnEl.value = "cancel";
    cancelBtnEl.id = "lm-signal-list-cancel";
    cancelBtnEl.className = "btn";
    cancelBtnEl.textContent = "取消";

    const saveBtnEl = doc.createElement("button");
    saveBtnEl.type = "button";
    saveBtnEl.id = "lm-signal-list-save";
    saveBtnEl.className = "btn btn-primary";
    saveBtnEl.textContent = "保存默认信号集";

    menu.appendChild(cancelBtnEl);
    menu.appendChild(saveBtnEl);
    form.appendChild(heading);
    form.appendChild(desc);
    form.appendChild(textarea);
    form.appendChild(status);
    form.appendChild(menu);

    if (typeof dialog.replaceChildren === "function") {
      dialog.replaceChildren(form);
    } else if (typeof dialog.appendChild === "function") {
      dialog.appendChild(form);
    }
    doc.body.appendChild(dialog);

    const cancelBtn = doc.getElementById("lm-signal-list-cancel");
    if (cancelBtn && typeof cancelBtn.addEventListener === "function") {
      cancelBtn.addEventListener("click", () => {
        if (typeof dialog.close === "function") dialog.close();
        else dialog.hidden = true;
      });
    }
    const saveBtn = doc.getElementById("lm-signal-list-save");
    if (saveBtn && typeof saveBtn.addEventListener === "function") {
      saveBtn.addEventListener("click", async () => {
        const input = doc.getElementById("lm-default-signal-list") as HTMLTextAreaElement | null;
        const text = input ? String(input.value || "") : "";
        await saveDefaultSignalList(text);
      });
    }
    return dialog;
  }

  async function openSignalListModal(): Promise<void> {
    const dialog = ensureSignalListDialog();
    const api = resolveApi();
    const pid = resolveProjectId();
    const textarea = doc && doc.getElementById
      ? (doc.getElementById("lm-default-signal-list") as HTMLTextAreaElement | null)
      : null;
    const statusEl = doc && doc.getElementById ? doc.getElementById("lm-signal-list-status") : null;
    if (statusEl) statusEl.textContent = "";

    if (api && typeof api.getProject === "function" && pid > 0) {
      try {
        const data = await api.getProject(pid);
        const proj: QualificationProjectInfo = (data && data.project) || data || {};
        cachedSignalList = String(proj.default_signal_list || "");
      } catch (_e) { /* keep cached value */ }
    }
    if (textarea) textarea.value = cachedSignalList;
    if (dialog) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.hidden = false;
    }
  }

  async function saveDefaultSignalList(signalListText: string): Promise<any> {
    const api = resolveApi();
    const ui = resolveUi();
    const pid = resolveProjectId();
    if (!api || typeof api.patchProject !== "function" || pid <= 0) return null;

    const normalized = String(signalListText || "")
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .join("\n");

    const statusEl = doc && doc.getElementById ? doc.getElementById("lm-signal-list-status") : null;
    try {
      const res = await api.patchProject(pid, { default_signal_list: normalized });
      cachedSignalList = normalized;
      if (statusEl) statusEl.textContent = "已保存默认信号集";
      if (ui && typeof ui.toast === "function") ui.toast("默认信号集已保存", true);
      const dialog = doc && doc.getElementById ? doc.getElementById("lm-signal-list-dialog") : null;
      if (dialog) {
        if (typeof dialog.close === "function") dialog.close();
        else dialog.hidden = true;
      }
      return res;
    } catch (err: any) {
      const msg = (err && err.message) || "保存失败";
      if (statusEl) statusEl.textContent = msg;
      if (ui && typeof ui.toast === "function") ui.toast(msg, false);
      throw err;
    }
  }

  function wrapApi(targetApi?: any): void {
    const api = resolveApi(targetApi);
    if (!api) return;

    if (typeof api.runSelectedTasks === "function" && !(api.runSelectedTasks as any).__lmQualWrapped) {
      const origRun = api.runSelectedTasks;
      const wrappedRun = function (this: any, id: number, payload?: RunSelectedPayload) {
        const cleanRoom = isCleanRoomEnabled();
        const nextPayload: RunSelectedPayload = Object.assign({}, payload || {});
        if (cleanRoom) {
          nextPayload.clean_room = true;
        } else {
          delete nextPayload.clean_room;
        }
        return origRun.call(this, id, nextPayload);
      };
      (wrappedRun as any).__lmQualWrapped = true;
      api.runSelectedTasks = wrappedRun;
    }

    if (typeof api.rerunSelectedTasks === "function" && !(api.rerunSelectedTasks as any).__lmQualWrapped) {
      const origRerun = api.rerunSelectedTasks;
      const wrappedRerun = function (this: any, id: number, keysOrPayload: any, opts?: any) {
        const cleanRoom = isCleanRoomEnabled();
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
        if (opts !== undefined) {
          const nextOpts = Object.assign({}, opts);
          delete nextOpts.clean_room;
          return origRerun.call(this, id, keysOrPayload, nextOpts);
        }
        return origRerun.call(this, id, keysOrPayload);
      };
      (wrappedRerun as any).__lmQualWrapped = true;
      api.rerunSelectedTasks = wrappedRerun;
    }
  }

  function init(): void {
    ensureToolbarControls();
    wrapApi();
  }

  const controller: EditorQualificationController = {
    init,
    syncVisibility,
    isCleanRoomEnabled,
    setCleanRoomEnabled,
    wrapApi,
    openSignalListModal,
    saveDefaultSignalList,
  };

  if (win) {
    win.LMEditorQualification = controller;
  }
  (globalThis as any).LMEditorQualification = controller;

  if (doc && doc.readyState === "loading" && typeof doc.addEventListener === "function") {
    doc.addEventListener("DOMContentLoaded", init);
    wrapApi();
  } else {
    init();
  }
})(typeof window !== "undefined" ? window : globalThis, typeof document !== "undefined" ? document : null);
