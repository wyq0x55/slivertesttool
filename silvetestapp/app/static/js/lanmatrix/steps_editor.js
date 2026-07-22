/* Graphical step-table editor for one test item's ``steps`` field.
 *
 * Edits the columnar JSON document the Test-Matrix codec produces:
 *   { input_signals:  [[name, path], ...],
 *     expected_signals:[[name, path], ...],
 *     steps: [{ no, purpose, operation, subroutine, args,
 *               inputs:[...], expecteds:[...], timing }, ...] }
 *
 * This is the storage/authoring shape. It is converted to the executable
 * ``silver_json_runner`` test-case schema server-side (see
 * ``services/lanmatrix/silver_json_export.py``) at queue time, so what the user
 * edits here stays 1:1 with the Excel 手順 table while the runner still gets the
 * exact JSON it expects.
 *
 * On save it serialises back to JSON and hands it to the caller, which PATCHes
 * the item's ``steps`` cell through the normal optimistic-locked item API.
 * Fully offline, dependency-free.
 */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  const EMPTY = { input_signals: [], expected_signals: [], steps: [] };

  function parseDoc(raw) {
    let doc = null;
    if (raw && typeof raw === "object") doc = raw;
    else if (typeof raw === "string" && raw.trim()) {
      try { doc = JSON.parse(raw); } catch (e) { doc = null; }
    }
    doc = doc || {};
    return {
      input_signals: normSignals(doc.input_signals),
      expected_signals: normSignals(doc.expected_signals),
      steps: (Array.isArray(doc.steps) ? doc.steps : []).map(normStep),
    };
  }

  function normSignals(list) {
    if (!Array.isArray(list)) return [];
    return list.map((s) => {
      if (Array.isArray(s)) return [s[0] == null ? "" : String(s[0]), s[1] == null ? "" : String(s[1])];
      if (s && typeof s === "object") return [s.name || "", s.path || ""];
      return [s == null ? "" : String(s), ""];
    });
  }

  function normStep(s) {
    s = s || {};
    return {
      no: s.no == null ? "" : s.no,
      purpose: s.purpose == null ? "" : s.purpose,
      operation: s.operation == null ? "" : s.operation,
      subroutine: s.subroutine == null ? "" : s.subroutine,
      args: s.args == null ? "" : s.args,
      inputs: Array.isArray(s.inputs) ? s.inputs.slice() : [],
      expecteds: Array.isArray(s.expecteds) ? s.expecteds.slice() : [],
      timing: s.timing == null ? "" : s.timing,
    };
  }

  class StepsEditor {
    constructor(dialog) {
      this.dialog = dialog;
      this.host = dialog.querySelector("#lm-steps-body");
      this.titleEl = dialog.querySelector("#lm-steps-title");
      this.statusEl = dialog.querySelector("#lm-steps-task-status");
      this.enqueueBtn = dialog.querySelector("#lm-steps-enqueue");
      this.errEl = dialog.querySelector("#lm-steps-error");
      this.doc = Object.assign({}, EMPTY);
      this.onSave = null;
      this.onEnqueue = null;
      this.getStatus = null;
      this.testId = "";
      // Rendering engine: built-in HTML tables by default; upgraded to a Univer
      // Sheets view when the vendored bundle exposes window.LMUniverSteps.mount.
      this.view = null;
      this.engine = "builtin";
      this.backdrop = null;
      // Lib/Const reference search panel (lazily populated on open()).
      this.refPanel = null;
      this.refEl = dialog.querySelector("#lm-steps-ref");
      this.refToggleBtn = dialog.querySelector("#lm-steps-ref-toggle");
      this.loadRef = null;
      this._refLoaded = false;
      this._initRefPanel();
      this._wire();
      // The dialog is opened NON-modally (see open()). A native modal <dialog>
      // lives in the top layer, but Univer renders its cell editor / overlays in
      // a portal appended to document.body — which sits *below* the top layer and
      // is therefore inert, so cells look visible but can't be typed into. Opening
      // non-modally keeps Univer's editor in the same, interactive stacking
      // context. We supply our own backdrop and hide it whenever the dialog closes.
      this.dialog.addEventListener("close", () => {
        this._hideBackdrop();
        // The Univer workbook is now REUSED across items instead of being torn
        // down and recreated on every open (creating a fresh Univer instance is
        // expensive and sometimes failed to display). setDoc() fully resets the
        // sheet + table geometry, so switching to an item with a different signal
        // count is correct without a rebuild. The view is kept mounted (hidden
        // with the dialog) and re-shown on the next open(). See open().
      });
      // Non-modal dialogs don't auto-close on Esc; wire it ourselves.
      this.dialog.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { e.preventDefault(); this.dialog.close(); }
      });
    }

    _showBackdrop() {
      if (!this.backdrop) {
        const b = document.createElement("div");
        b.className = "lm-steps-backdrop";
        b.addEventListener("click", () => { this.dialog.close(); });
        this.backdrop = b;
      }
      if (!this.backdrop.isConnected) document.body.appendChild(this.backdrop);
    }

    _hideBackdrop() {
      if (this.backdrop && this.backdrop.isConnected) this.backdrop.remove();
    }

    // ---- Lib/Const reference panel ------------------------------------- //

    _initRefPanel() {
      if (!this.refEl || !window.LMStepsRefPanel) return;
      try { this.refPanel = new window.LMStepsRefPanel(this.refEl); } catch (_e) { this.refPanel = null; }
      if (this.refToggleBtn) {
        this.refToggleBtn.addEventListener("click", () => this._toggleRef());
      }
    }

    _toggleRef() {
      if (!this.refEl || !this.refPanel) return;
      const show = this.refEl.hidden;
      this.refEl.hidden = !show;
      if (this.refToggleBtn) this.refToggleBtn.classList.toggle("is-on", show);
      // The Univer canvas shares the flex row with the panel; nudge a relayout
      // so it reflows to the width freed/taken by the panel.
      if (this.view && typeof this.view.resize === "function") {
        requestAnimationFrame(() => { try { this.view.resize(); } catch (_e) { /* noop */ } });
      }
      if (show) {
        this._loadRefData();
        this.refPanel.focusSearch();
      }
    }

    // Pull the current project's Lib/Const rows via the host-provided callback
    // and hand them to BOTH the search panel (feature A) and the Univer view
    // (feature C: サブルーチン hover definitions). Cached per open() so repeated
    // toggles are free; reset on each open() so lib/const edits are reflected.
    _loadRefData() {
      if (!this.loadRef || this._refLoaded) return;
      this._refLoaded = true;
      Promise.resolve()
        .then(() => this.loadRef())
        .then((data) => {
          data = data || {};
          if (this.refPanel) this.refPanel.setData(data);
          if (this.view && typeof this.view.setRefData === "function") {
            try { this.view.setRefData(data); } catch (_e) { /* best-effort */ }
          }
        })
        .catch(() => { this._refLoaded = false; });
    }

    // Lazily mount the Univer steps view into the dialog body. Falls back to the
    // built-in tables on any failure. A fresh view is mounted per open() (the
    // previous one is destroyed on close), so each item gets its own Univer
    // workbook and never inherits the prior item's sheet/table geometry.
    _ensureView() {
      if (this.view) return;
      const mgr = global.LMUniverSteps;
      if (mgr && typeof mgr.mount === "function") {
        try {
          this.host.innerHTML = "";
          this.view = mgr.mount(this.host, {});
          this.engine = "univer";
        } catch (e) {
          console.warn("Univer steps mount failed, using built-in editor:", e);
          this.view = null;
          this.engine = "builtin";
        }
      }
    }

    // Dispose the current Univer workbook (if any) and clear the dialog body so
    // the next open() mounts a clean instance. No-op for the built-in tables.
    _destroyView() {
      if (this.view) {
        try {
          if (typeof this.view.dispose === "function") this.view.dispose();
        } catch (e) { /* best-effort teardown */ }
      }
      this.view = null;
      this.engine = "builtin";
      if (this.host) this.host.innerHTML = "";
    }

    // Pull the latest edits out of the active view into this.doc so toolbar
    // mutations (add row/column) and save never lose in-cell changes. No-op for
    // the built-in tables, which mutate this.doc live via input events.
    _pull() {
      if (this.view && typeof this.view.getDoc === "function") {
        try { this.doc = parseDoc(this.view.getDoc()); } catch (e) { /* keep this.doc */ }
      }
    }

    _wire() {
      const self = this;
      this.dialog.querySelector("#lm-steps-save")
        .addEventListener("click", (e) => { e.preventDefault(); self._save(); });
      const fs = this.dialog.querySelector("#lm-steps-fullscreen");
      if (fs) fs.addEventListener("click", (e) => { e.preventDefault(); self._toggleFullscreen(); });
      if (this.enqueueBtn) {
        this.enqueueBtn.addEventListener("click", (e) => { e.preventDefault(); self._enqueue(); });
      }
    }

    // Render the current run status of this item's test task next to the title.
    // ``status`` null/empty means the test has never been queued -> "noTask".
    _setStatus(status) {
      if (!this.statusEl) return;
      const s = status ? String(status) : "";
      this.statusEl.hidden = false;
      this.statusEl.className = "lm-badge " + (s ? "lm-status-" + s : "lm-status-notask");
      this.statusEl.textContent = s || "noTask";
    }

    async _refreshStatus() {
      // Status is only meaningful for rows that carry a test_id (the test sheet).
      if (!this.testId || typeof this.getStatus !== "function") {
        if (this.statusEl) this.statusEl.hidden = true;
        return;
      }
      this._setStatus(null);
      try {
        const status = await this.getStatus(this.testId);
        this._setStatus(status);
      } catch (e) {
        this._setStatus(null);
      }
    }

    async _enqueue() {
      if (!this.testId || typeof this.onEnqueue !== "function") return;
      const btn = this.enqueueBtn;
      if (btn) btn.disabled = true;
      this.errEl.hidden = true;
      try {
        await this.onEnqueue(this.testId);
        this._setStatus("queued");
      } catch (ex) {
        this.errEl.textContent = (ex && ex.message) || "入队失败";
        this.errEl.hidden = false;
      } finally {
        if (btn) btn.disabled = false;
        this._refreshStatus();
      }
    }

    // Toggle between the bottom drawer and a full-screen editor. Univer needs a
    // resize nudge after the container size changes so its canvas re-lays out.
    _toggleFullscreen() {
      const full = this.dialog.classList.toggle("lm-steps-drawer-full");
      const btn = this.dialog.querySelector("#lm-steps-fullscreen");
      if (btn) btn.textContent = full ? "🗗 退出全屏" : "⛶ 全屏";
      if (this.view && typeof this.view.resize === "function") {
        try { this.view.resize(); } catch (e) { /* ignore */ }
      } else {
        try { window.dispatchEvent(new Event("resize")); } catch (e) { /* ignore */ }
      }
    }

    open(item, opts) {
      this.item = item;
      this.onSave = (opts && opts.onSave) || null;
      this.onEnqueue = (opts && opts.onEnqueue) || null;
      this.getStatus = (opts && opts.getStatus) || null;
      this.loadRef = (opts && opts.loadRef) || null;
      // Re-fetch lib/const on each open so edits elsewhere are reflected; if the
      // panel is already visible, refresh it now, otherwise it loads on toggle.
      this._refLoaded = false;
      this.testId = (opts && opts.testId != null) ? String(opts.testId).trim() : "";
      this.fieldKey = (opts && opts.fieldKey) || "steps";
      this.doc = parseDoc(item[this.fieldKey]);
      this.errEl.hidden = true;
      // The enqueue button + status badge only apply to rows with a test_id.
      const canEnqueue = !!(this.testId && this.onEnqueue);
      if (this.enqueueBtn) this.enqueueBtn.hidden = !canEnqueue;
      if (this.enqueueBtn) this.enqueueBtn.disabled = false;
      this._refreshStatus();
      // Always reopen as the bottom drawer (not full-screen).
      this.dialog.classList.remove("lm-steps-drawer-full");
      const fsBtn = this.dialog.querySelector("#lm-steps-fullscreen");
      if (fsBtn) fsBtn.textContent = "⛶ 全屏";
      this.titleEl.textContent = item.case_id || item.title || `#${item.id}`;
      this._syncStepArity();
      // Show NON-modally (with our own backdrop) so Univer's cell editor stays
      // interactive — see the constructor note. Show first so the Univer canvas
      // mounts with a non-zero size, then render.
      this._showBackdrop();
      this.dialog.show();
      // Reuse the mounted Univer workbook across items (mounted once, lazily).
      // setDoc() (called by render()) resets the sheet + table geometry, so the
      // reused view renders this item correctly without a costly re-create.
      this._ensureView();
      this.render();
      // The dialog is display:none while closed, so a reused Univer canvas may
      // have been laid out against a zero-size host. Nudge a relayout once the
      // re-shown dialog has non-zero size so the grid paints on reopen.
      if (this.view && typeof this.view.resize === "function") {
        requestAnimationFrame(() => {
          try { this.view.resize(); } catch (e) { /* best-effort */ }
        });
      }
      // Load lib/const references for this open: feeds the サブルーチン hover
      // definitions (feature C) even when the search panel is never opened, and
      // refreshes the panel too if it happens to be visible.
      this._loadRefData();
    }

    _addStep() {
      this.doc.steps.push({
        no: this.doc.steps.length + 1, purpose: "", operation: "",
        subroutine: "", args: "",
        inputs: this.doc.input_signals.map(() => ""),
        expecteds: this.doc.expected_signals.map(() => ""),
        timing: "",
      });
    }

    _syncStepArity() {
      const ni = this.doc.input_signals.length;
      const ne = this.doc.expected_signals.length;
      this.doc.steps.forEach((s) => {
        while (s.inputs.length < ni) s.inputs.push("");
        s.inputs.length = ni;
        while (s.expecteds.length < ne) s.expecteds.push("");
        s.expecteds.length = ne;
      });
    }

    render() {
      // Univer view owns the dialog body: hand it the doc and stop (never wipe
      // its canvas with innerHTML).
      if (this.view) { this.view.setDoc(this.doc); return; }
      this.host.innerHTML =
        this._signalsTable("input") +
        this._signalsTable("expected") +
        this._stepsTable();
      this._bind();
    }

    _signalsTable(kind) {
      const list = kind === "input" ? this.doc.input_signals : this.doc.expected_signals;
      const label = kind === "input" ? "入力値 (输入信号)" : "期待値 (期望信号)";
      const rows = list.map((sig, i) => `
        <tr>
          <td class="lm-se-idx">${i + 1}</td>
          <td><input class="lm-input lm-se-cell" data-kind="${kind}" data-i="${i}" data-f="0" value="${esc(sig[0])}" placeholder="信号名"></td>
          <td><input class="lm-input lm-se-cell" data-kind="${kind}" data-i="${i}" data-f="1" value="${esc(sig[1])}" placeholder="路径 / path"></td>
          <td><button class="lm-btn lm-btn-sm lm-se-del" data-kind="${kind}" data-i="${i}">删除</button></td>
        </tr>`).join("");
      return `<div class="lm-se-block">
        <h4>${label}</h4>
        <table class="lm-table lm-se-table">
          <thead><tr><th>#</th><th>名称</th><th>路径</th><th></th></tr></thead>
          <tbody>${rows || `<tr><td colspan="4" class="lm-muted">无</td></tr>`}</tbody>
        </table>
        <div class="lm-se-actions">
          <button class="lm-btn lm-btn-sm lm-se-add" data-kind="${kind}">+ 添加信号</button>
        </div></div>`;
    }

    _stepsTable() {
      const inNames = this.doc.input_signals.map((s) => s[0] || "入力");
      const exNames = this.doc.expected_signals.map((s) => s[0] || "期待");
      const head =
        `<th class="lm-st-no">手順番号</th><th>手順目的</th><th>操作手順</th>` +
        `<th>サブルーチン</th><th>引数</th>` +
        inNames.map((n) => `<th class="lm-st-sig-in">入力: ${esc(n)}</th>`).join("") +
        exNames.map((n) => `<th class="lm-st-sig-ex">期待: ${esc(n)}</th>`).join("") +
        `<th>確認タイミング</th><th></th>`;
      const rows = this.doc.steps.map((s, i) => {
        const inCells = s.inputs.map((v, j) =>
          `<td><input class="lm-input lm-st-cell" data-i="${i}" data-arr="inputs" data-j="${j}" value="${esc(v)}"></td>`).join("");
        const exCells = s.expecteds.map((v, j) =>
          `<td><input class="lm-input lm-st-cell" data-i="${i}" data-arr="expecteds" data-j="${j}" value="${esc(v)}"></td>`).join("");
        return `<tr>
          <td><input class="lm-input lm-st-cell lm-st-no" data-i="${i}" data-f="no" value="${esc(s.no)}"></td>
          <td><input class="lm-input lm-st-cell" data-i="${i}" data-f="purpose" value="${esc(s.purpose)}"></td>
          <td><input class="lm-input lm-st-cell" data-i="${i}" data-f="operation" value="${esc(s.operation)}"></td>
          <td><input class="lm-input lm-st-cell" data-i="${i}" data-f="subroutine" value="${esc(s.subroutine)}"></td>
          <td><input class="lm-input lm-st-cell" data-i="${i}" data-f="args" value="${esc(s.args)}"></td>
          ${inCells}${exCells}
          <td><input class="lm-input lm-st-cell" data-i="${i}" data-f="timing" value="${esc(s.timing)}"></td>
          <td><button class="lm-btn lm-btn-sm lm-st-del" data-i="${i}">删除</button></td>
        </tr>`;
      }).join("");
      const colspan = 7 + inNames.length + exNames.length;
      return `<div class="lm-st-block">
        <h4>手順 (测试步骤)</h4>
        <div class="lm-st-scroll">
        <table class="lm-table lm-st-table">
          <thead><tr>${head}</tr></thead>
          <tbody>${rows || `<tr><td colspan="${colspan}" class="lm-muted">暂无步骤。</td></tr>`}</tbody>
        </table></div>
        <div class="lm-st-actions">
          <button class="lm-btn lm-btn-sm lm-st-add">+ 添加步骤</button>
        </div></div>`;
    }

    _bind() {
      const self = this;
      this.host.querySelectorAll(".lm-se-cell").forEach((el) => {
        el.addEventListener("input", () => {
          const kind = el.dataset.kind;
          const list = kind === "input" ? self.doc.input_signals : self.doc.expected_signals;
          list[Number(el.dataset.i)][Number(el.dataset.f)] = el.value;
        });
        // Signal name (data-f="0") feeds the 手順 (steps) column headers
        // ("入力: <name>" / "期待: <name>"). Re-render on commit (blur / Enter)
        // so the step table columns stay in sync — but not on every keystroke,
        // which would steal focus mid-typing.
        el.addEventListener("change", () => {
          if (el.dataset.f === "0") self.render();
        });
      });
      // Add an input / expected signal, then grow every step row's cell arity
      // and re-render so the new 手順 column appears immediately.
      this.host.querySelectorAll(".lm-se-add").forEach((el) => {
        el.addEventListener("click", (e) => {
          e.preventDefault();
          const kind = el.dataset.kind;
          const list = kind === "input" ? self.doc.input_signals : self.doc.expected_signals;
          list.push(["", ""]);
          self._syncStepArity();
          self.render();
        });
      });
      this.host.querySelectorAll(".lm-st-add").forEach((el) => {
        el.addEventListener("click", (e) => {
          e.preventDefault();
          self._addStep();
          self.render();
        });
      });
      this.host.querySelectorAll(".lm-se-del").forEach((el) => {
        el.addEventListener("click", (e) => {
          e.preventDefault();
          const kind = el.dataset.kind;
          const list = kind === "input" ? self.doc.input_signals : self.doc.expected_signals;
          list.splice(Number(el.dataset.i), 1);
          self._syncStepArity();
          self.render();
        });
      });
      this.host.querySelectorAll(".lm-st-cell").forEach((el) => {
        el.addEventListener("input", () => {
          const step = self.doc.steps[Number(el.dataset.i)];
          if (el.dataset.arr) step[el.dataset.arr][Number(el.dataset.j)] = el.value;
          else step[el.dataset.f] = el.value;
        });
      });
      this.host.querySelectorAll(".lm-st-del").forEach((el) => {
        el.addEventListener("click", (e) => {
          e.preventDefault();
          self.doc.steps.splice(Number(el.dataset.i), 1);
          self.render();
        });
      });
    }

    _serialize() {
      const steps = this.doc.steps.map((s) => {
        const noNum = String(s.no).trim();
        const asInt = /^-?\d+$/.test(noNum) ? Number(noNum) : (s.no === "" ? null : s.no);
        return {
          no: asInt,
          purpose: s.purpose || null,
          operation: s.operation || null,
          subroutine: s.subroutine || null,
          args: s.args || null,
          inputs: s.inputs.map((v) => (v === "" ? null : v)),
          expecteds: s.expecteds.map((v) => (v === "" ? null : v)),
          timing: s.timing || null,
        };
      });
      return {
        input_signals: this.doc.input_signals.map((s) => [s[0] || "", s[1] || ""]),
        expected_signals: this.doc.expected_signals.map((s) => [s[0] || "", s[1] || ""]),
        steps,
      };
    }

    async _save() {
      this.errEl.hidden = true;
      this._pull();
      const doc = this._serialize();
      const json = JSON.stringify(doc, null, 2);
      const btn = this.dialog.querySelector("#lm-steps-save");
      btn.disabled = true;
      try {
        if (this.onSave) await this.onSave(json, doc);
        this.dialog.close();
      } catch (ex) {
        this.errEl.textContent = (ex && ex.message) || "保存失败";
        this.errEl.hidden = false;
      } finally {
        btn.disabled = false;
      }
    }
  }

  const LMStepsEditor = {
    _instance: null,
    open(item, opts) {
      const dialog = document.getElementById("lm-steps-dialog");
      if (!dialog) { console.warn("steps dialog missing"); return; }
      if (!this._instance || this._instance.dialog !== dialog) {
        this._instance = new StepsEditor(dialog);
      }
      this._instance.open(item, opts);
    },
    parseDoc,
  };

  global.LMStepsEditor = LMStepsEditor;
})(window);
