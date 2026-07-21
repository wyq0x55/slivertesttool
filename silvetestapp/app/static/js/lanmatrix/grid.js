/* Editable grid engine for the LAN Test Matrix.
 *
 * Ships a dependency-free, fully-offline editable grid (FallbackGrid) that
 * supports dynamic columns, per-cell data-type editors, dropdowns for select
 * fields, read-only cells, validation hints (help text / errors), comment
 * markers and optimistic-lock-aware saves.
 *
 * If a vendored Univer Sheets bundle is present under static/vendor/univer/
 * (window.LMUniver.mount), it is used instead — same data contract — so the
 * platform can upgrade to Univer without touching the rest of the app. Both
 * paths are non-commercial-license (Univer Apache-2.0; this grid is bundled).
 */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  class FallbackGrid {
    /* opts: { host, fields, onSave(item, changes)->Promise, onComment(item, fieldKey),
     *         onSteps(item), onDelete(item),
     *         onSelectionChange(ids), onInsert(item, "above"|"below"),
     *         onBulkDelete(ids), onBulkDuplicate(ids), onMove(ids, "up"|"down") } */
    constructor(opts) {
      this.host = opts.host;
      this.fields = opts.fields || [];
      this.onSave = opts.onSave || (() => Promise.resolve());
      this.onComment = opts.onComment || (() => {});
      this.onSteps = opts.onSteps || null;
      this.onDelete = opts.onDelete || null;
      this.onSelectionChange = opts.onSelectionChange || (() => {});
      this.onInsert = opts.onInsert || null;
      this.onBulkDelete = opts.onBulkDelete || null;
      this.onBulkDuplicate = opts.onBulkDuplicate || null;
      this.onMove = opts.onMove || null;
      this.items = [];
      this.selected = new Set();
      this._lastClickedId = null;
      this.engine = "builtin";
      this._menuEl = null;
      this._bindGlobalDismiss();
    }

    _hasActions() { return !!(this.onSteps || this.onDelete); }
    _hasStepsField() { return (this.fields || []).some((f) => f.data_type === "steps"); }

    setFields(fields) { this.fields = fields; }

    setData(items) {
      this.items = items || [];
      // Drop selections for rows no longer present (e.g. after a reload).
      const present = new Set(this.items.map((i) => i.id));
      this.selected.forEach((id) => { if (!present.has(id)) this.selected.delete(id); });
      this.render();
      this._emitSelection();
    }

    // Mark cells the server rejected during materialization (design §12.2).
    // ``map`` is ``{ rowId: { cells: [field_key...], message } }``; when ``cells``
    // is empty the whole row is flagged. All other cells are cleared. Uses a
    // dedicated class so it never collides with the transient save-time
    // ``lm-cell-error`` mark. Re-applied after every render.
    setCellErrors(map) {
      this._cellErrors = map || {};
      this._drawCellErrors();
    }

    _drawCellErrors() {
      const map = this._cellErrors || {};
      this.host.querySelectorAll("td.lm-cell.lm-cell-collab-error").forEach((td) => {
        td.classList.remove("lm-cell-collab-error");
        td.removeAttribute("data-collab-error");
      });
      Object.keys(map).forEach((id) => {
        const tr = this.host.querySelector(`tbody tr[data-id="${id}"]`);
        if (!tr) return;
        const err = map[id] || {};
        const cells = err.cells || [];
        const msg = err.message || "服务器校验未通过";
        const targets = cells.length
          ? cells.map((k) => tr.querySelector(`td.lm-cell[data-key="${k}"]`))
          : Array.from(tr.querySelectorAll("td.lm-cell"));
        targets.forEach((td) => {
          if (!td) return;
          td.classList.add("lm-cell-collab-error");
          td.setAttribute("data-collab-error", msg);
          td.title = msg;
        });
      });
    }

    // True while the user is actively editing a cell (a contenteditable text
    // cell or a select/date control has focus). Lets the editor freeze remote
    // real-time applies only during a genuine edit — matching the Univer
    // adapter's isEditing() so both engines gate sync the same, accurate way.
    isEditing() {
      const ae = document.activeElement;
      if (!ae || !this.host.contains(ae)) return false;
      if (ae.isContentEditable && ae.classList.contains("lm-cell")) return true;
      return ae.classList.contains("lm-cell-control");
    }

    // Current editing cell as ``{ id, col }`` (col = index into visible fields,
    // or null when only a row is focused). Cached as ``_lastActive`` so it
    // survives blur — the editor publishes it into awareness as the local cursor
    // so peers can draw a precise remote-cursor overlay (design §6.1).
    getActiveCell() {
      const ae = document.activeElement;
      if (!ae || !this.host.contains(ae)) return this._lastActive || null;
      const tr = ae.closest && ae.closest("tr[data-id]");
      if (!tr) return this._lastActive || null;
      const id = Number(tr.dataset.id);
      let col = null;
      const td = ae.closest && ae.closest("td.lm-cell");
      if (td && td.dataset.key) {
        const fields = this._visibleFields();
        const i = fields.findIndex((f) => f.field_key === td.dataset.key);
        col = i >= 0 ? i : null;
      }
      this._lastActive = { id: id, col: col };
      return this._lastActive;
    }

    // Draw remote collaborators' cursors/selections as an absolutely-positioned
    // DOM overlay over the grid (design §6.1). ``cursors`` is a list of
    // ``{ id, col, name, color }``. Returns true — the DOM overlay is always
    // available in this (fallback) grid. The Univer canvas engine returns false
    // and simply shows no presence marker (the row-highlight fallback was
    // removed because it stayed permanently lit).
    setRemoteCursors(cursors) {
      this._remoteCursors = Array.isArray(cursors) ? cursors : [];
      this._drawRemoteCursors();
      return true;
    }

    _ensureOverlay() {
      if (getComputedStyle(this.host).position === "static") {
        this.host.style.position = "relative";
      }
      let ov = this.host.querySelector(":scope > .lm-collab-overlay");
      if (!ov) {
        ov = document.createElement("div");
        ov.className = "lm-collab-overlay";
        this.host.appendChild(ov);
      }
      return ov;
    }

    // Position a box over the target cell (or whole row when col is unknown)
    // using content-relative offsets, so the overlay scrolls with the grid for
    // free — no scroll handler needed. Re-run after every render (innerHTML wipes
    // the layer). Rows filtered out of the current view are simply skipped.
    _drawRemoteCursors() {
      const list = this._remoteCursors || [];
      const ov = this._ensureOverlay();
      ov.innerHTML = "";
      if (!list.length) return;
      const fields = this._visibleFields();
      list.forEach((c) => {
        const tr = this.host.querySelector(`tbody tr[data-id="${c.id}"]`);
        if (!tr) return;
        let target = null;
        if (c.col != null && fields[c.col]) {
          target = tr.querySelector(`td.lm-cell[data-key="${fields[c.col].field_key}"]`);
        }
        const rect = target || tr;
        const box = document.createElement("div");
        box.className = "lm-collab-cursor" + (target ? "" : " lm-collab-cursor-row");
        box.style.setProperty("--lm-collab-color", c.color || "#888");
        box.style.left = rect.offsetLeft + "px";
        box.style.top = rect.offsetTop + "px";
        box.style.width = rect.offsetWidth + "px";
        box.style.height = rect.offsetHeight + "px";
        const tag = document.createElement("span");
        tag.className = "lm-collab-cursor-tag";
        tag.textContent = c.name || "协作者";
        box.appendChild(tag);
        ov.appendChild(box);
      });
    }

    // --- Selection -------------------------------------------------------- #
    getSelectedIds() {
      return this.items.map((i) => i.id).filter((id) => this.selected.has(id));
    }

    clearSelection() {
      this.selected.clear();
      this.host.querySelectorAll("tr.lm-row-selected").forEach((tr) =>
        tr.classList.remove("lm-row-selected"));
      this.host.querySelectorAll(".lm-row-sel").forEach((cb) => { cb.checked = false; });
      const all = this.host.querySelector(".lm-sel-all");
      if (all) { all.checked = false; all.indeterminate = false; }
      this._emitSelection();
    }

    _emitSelection() {
      try { this.onSelectionChange(this.getSelectedIds()); } catch (e) { /* noop */ }
    }

    _setRowSelected(id, on) {
      if (on) this.selected.add(id); else this.selected.delete(id);
      const tr = this.host.querySelector(`tr[data-id="${id}"]`);
      if (tr) {
        tr.classList.toggle("lm-row-selected", on);
        const cb = tr.querySelector(".lm-row-sel");
        if (cb) cb.checked = on;
      }
    }

    _syncSelectAll() {
      const all = this.host.querySelector(".lm-sel-all");
      if (!all) return;
      const total = this.items.length;
      const n = this.getSelectedIds().length;
      all.checked = total > 0 && n === total;
      all.indeterminate = n > 0 && n < total;
    }

    _visibleFields() {
      return this.fields.filter((f) => f.is_active !== false);
    }

    render() {
      const fields = this._visibleFields();
      const head = fields.map((f) => {
        const req = f.is_required ? '<span class="lm-req">*</span>' : "";
        const ro = f.is_readonly ? ' 🔒' : "";
        return `<th title="${esc(f.help_text || "")}" data-key="${esc(f.field_key)}">${esc(f.display_name)}${req}${ro}</th>`;
      }).join("");

      const actHead = this._hasActions() ? '<th class="lm-actcol">操作</th>' : "";
      const selHead = '<th class="lm-selcol"><input type="checkbox" class="lm-sel-all" title="全选/取消"></th>';
      const span = fields.length + 2 + (this._hasActions() ? 1 : 0);
      const body = this.items.map((it) => this._rowHtml(it, fields)).join("");
      this.host.innerHTML = `
        <table class="lm-grid">
          <thead><tr>${selHead}<th class="lm-rownum">#</th>${head}${actHead}</tr></thead>
          <tbody>${body || `<tr><td colspan="${span}" class="lm-muted">暂无数据，点“新增行”或导入 Excel。</td></tr>`}</tbody>
        </table>`;
      this._bind();
      this._syncSelectAll();
      this._drawRemoteCursors();   // innerHTML wiped the overlay: repaint it (§6.1)
      this._drawCellErrors();      // innerHTML wiped cell marks: repaint them (§12.2)
    }

    _rowHtml(it, fields) {
      const cells = fields.map((f) => this._cellHtml(it, f)).join("");
      let act = "";
      if (this._hasActions()) {
        const steps = (this.onSteps && this._hasStepsField())
          ? `<button class="lm-btn lm-btn-sm lm-steps-btn" data-id="${it.id}">步骤明细</button>`
          : "";
        const del = this.onDelete
          ? `<button class="lm-btn lm-btn-sm lm-del-btn" data-id="${it.id}">删除行</button>`
          : "";
        act = `<td class="lm-actcol">${steps}${del}</td>`;
      }
      const on = this.selected.has(it.id);
      const sel = `<td class="lm-selcol"><input type="checkbox" class="lm-row-sel" data-id="${it.id}"${on ? " checked" : ""}></td>`;
      return `<tr data-id="${it.id}" data-version="${it.version}"${on ? ' class="lm-row-selected"' : ""}>
        ${sel}<td class="lm-rownum">${it.row_order || ""}</td>${cells}${act}</tr>`;
    }

    _cellHtml(it, f) {
      const raw = it[f.field_key];
      const cls = f.is_readonly ? "lm-cell lm-ro" : "lm-cell";
      const title = esc(f.help_text || "");
      const key = esc(f.field_key);
      const dt = f.data_type;
      const base = `class="${cls}" data-key="${key}" data-type="${esc(dt)}" title="${title}"`;

      // Read-only cells always render as static text.
      if (f.is_readonly) {
        let disp = Array.isArray(raw) ? raw.join("; ") : raw;
        return `<td ${base}>${esc(disp)}</td>`;
      }

      const opts = f.options || [];
      if (dt === "single_select" && opts.length) {
        const cur = raw == null ? "" : String(raw);
        const body = [""].concat(opts).map((o) =>
          `<option value="${esc(o)}"${o === cur ? " selected" : ""}>${o === "" ? "（空）" : esc(o)}</option>`
        ).join("");
        return `<td ${base}><select class="lm-cell-control" data-orig="${esc(cur)}">${body}</select></td>`;
      }
      if (dt === "boolean") {
        const cur = raw === true ? "是" : raw === false ? "否" : "";
        const body = ["", "是", "否"].map((o) =>
          `<option value="${o}"${o === cur ? " selected" : ""}>${o === "" ? "（空）" : o}</option>`
        ).join("");
        return `<td ${base}><select class="lm-cell-control" data-orig="${esc(cur)}">${body}</select></td>`;
      }
      if (dt === "multi_select" && opts.length) {
        const cur = Array.isArray(raw)
          ? raw.map(String)
          : (raw ? String(raw).split(/[;,]/).map((s) => s.trim()).filter(Boolean) : []);
        const body = opts.map((o) =>
          `<option value="${esc(o)}"${cur.indexOf(o) >= 0 ? " selected" : ""}>${esc(o)}</option>`
        ).join("");
        return `<td ${base}><select class="lm-cell-control" multiple data-orig="${esc(cur.join(";"))}">${body}</select></td>`;
      }
      if (dt === "date" || dt === "datetime") {
        const type = dt === "datetime" ? "datetime-local" : "date";
        let val = raw == null ? "" : String(raw);
        if (dt === "datetime" && val.length > 16) val = val.slice(0, 16);
        return `<td ${base}><input type="${type}" class="lm-cell-control" value="${esc(val)}" data-orig="${esc(val)}"></td>`;
      }

      // Test-procedure (手順) fields are edited through the "步骤明细" drawer, not
      // inline; the cell shows a compact read-only summary of the step count.
      if (dt === "steps") {
        let n = 0;
        try {
          const doc = typeof raw === "string" ? JSON.parse(raw) : raw;
          if (doc && Array.isArray(doc.steps)) n = doc.steps.length;
          else if (Array.isArray(doc)) n = doc.length;
        } catch (_e) { /* non-JSON or empty */ }
        const label = n ? `手順 ${n} 步` : "（点“步骤明细”编辑）";
        return `<td ${base} class="${cls} lm-ro">${esc(label)}</td>`;
      }

      // Default: free-text editing (text, multiline, integer, decimal, hex, url, user).
      let disp = Array.isArray(raw) ? raw.join("; ") : raw;
      return `<td ${base} contenteditable="plaintext-only">${esc(disp)}</td>`;
    }

    _bind() {
      const self = this;
      if (this.onSteps) {
        this.host.querySelectorAll(".lm-steps-btn").forEach((btn) => {
          btn.addEventListener("click", (e) => {
            e.preventDefault();
            const id = Number(btn.dataset.id);
            const item = self.items.find((x) => x.id === id);
            if (item) self.onSteps(item);
          });
        });
      }
      if (this.onDelete) {
        this.host.querySelectorAll(".lm-del-btn").forEach((btn) => {
          btn.addEventListener("click", (e) => {
            e.preventDefault();
            const id = Number(btn.dataset.id);
            const item = self.items.find((x) => x.id === id);
            if (item) self.onDelete(item);
          });
        });
      }
      // Free-text cells (contenteditable).
      this.host.querySelectorAll('td.lm-cell[contenteditable]').forEach((td) => {
        td.addEventListener("focus", () => {
          td.dataset.orig = td.textContent;
          self._emitSelection();   // republish local cursor (focused cell → col) §6.1
        });
        td.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); td.blur(); }
          if (e.key === "Escape") { td.textContent = td.dataset.orig; td.blur(); }
        });
        td.addEventListener("blur", () => self._commit(td, td.textContent, td.dataset.orig));
        td.addEventListener("dblclick", (e) => {
          if (e.altKey) {
            const tr = td.closest("tr");
            self.onComment({ id: Number(tr.dataset.id) }, td.dataset.key);
          }
        });
      });
      // Control cells (select / multi-select / date input): commit on change.
      this.host.querySelectorAll("td.lm-cell .lm-cell-control").forEach((ctl) => {
        ctl.addEventListener("focus", () => self._emitSelection());  // republish cursor col §6.1
        ctl.addEventListener("change", () => {
          const td = ctl.closest("td");
          let value;
          if (ctl.multiple) {
            value = Array.from(ctl.selectedOptions).map((o) => o.value).join(";");
          } else {
            value = ctl.value;
          }
          self._commit(td, value, ctl.dataset.orig, ctl);
        });
      });
      this._bindSelection();
      this._bindContextMenu();
    }

    // --- Selection wiring ------------------------------------------------- #
    _bindSelection() {
      const self = this;
      const all = this.host.querySelector(".lm-sel-all");
      if (all) {
        all.addEventListener("change", () => {
          const on = all.checked;
          self.items.forEach((it) => self._setRowSelected(it.id, on));
          all.indeterminate = false;
          self._emitSelection();
        });
      }
      this.host.querySelectorAll(".lm-row-sel").forEach((cb) => {
        cb.addEventListener("click", (e) => {
          const id = Number(cb.dataset.id);
          if (e.shiftKey && self._lastClickedId != null) {
            const ids = self.items.map((i) => i.id);
            let a = ids.indexOf(self._lastClickedId);
            let b = ids.indexOf(id);
            if (a > -1 && b > -1) {
              if (a > b) { const t = a; a = b; b = t; }
              for (let i = a; i <= b; i++) self._setRowSelected(ids[i], cb.checked);
            }
          } else {
            self._setRowSelected(id, cb.checked);
          }
          self._lastClickedId = id;
          self._syncSelectAll();
          self._emitSelection();
        });
      });
      // Clicking the row-number cell also toggles selection (Excel-like).
      this.host.querySelectorAll("td.lm-rownum").forEach((td) => {
        td.addEventListener("click", () => {
          const tr = td.closest("tr");
          const id = Number(tr.dataset.id);
          self._setRowSelected(id, !self.selected.has(id));
          self._lastClickedId = id;
          self._syncSelectAll();
          self._emitSelection();
        });
      });
    }

    // --- Right-click context menu ---------------------------------------- #
    _bindContextMenu() {
      const self = this;
      this.host.querySelectorAll("tbody tr[data-id]").forEach((tr) => {
        tr.addEventListener("contextmenu", (e) => {
          e.preventDefault();
          const id = Number(tr.dataset.id);
          // If the right-clicked row is not in the current selection, make it
          // the sole selection so the menu acts on what the user pointed at.
          if (!self.selected.has(id)) {
            self.clearSelection();
            self._setRowSelected(id, true);
            self._lastClickedId = id;
            self._syncSelectAll();
            self._emitSelection();
          }
          const item = self.items.find((x) => x.id === id);
          self._showContextMenu(e.clientX, e.clientY, item);
        });
      });
    }

    _bindGlobalDismiss() {
      const hide = () => this._hideContextMenu();
      document.addEventListener("click", hide);
      document.addEventListener("scroll", hide, true);
      window.addEventListener("resize", hide);
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
    }

    _hideContextMenu() {
      if (this._menuEl) { this._menuEl.remove(); this._menuEl = null; }
    }

    _showContextMenu(x, y, item) {
      this._hideContextMenu();
      const n = this.getSelectedIds().length;
      const rows = [];
      if (this.onInsert) {
        rows.push(["above", `在上方插入行`, false]);
        rows.push(["below", `在下方插入行`, false]);
      }
      if (this.onBulkDuplicate) rows.push(["dup", `复制所选 (${n})`, n === 0]);
      if (this.onBulkDelete) rows.push(["del", `删除所选 (${n})`, n === 0]);
      if (this.onMove) {
        rows.push(["up", `上移`, n === 0]);
        rows.push(["down", `下移`, n === 0]);
      }
      if (!rows.length) return;
      const menu = document.createElement("ul");
      menu.className = "lm-ctxmenu";
      menu.innerHTML = rows.map(([act, label, disabled]) =>
        `<li data-act="${act}"${disabled ? ' class="lm-disabled"' : ""}>${esc(label)}</li>`
      ).join("");
      document.body.appendChild(menu);
      // Keep the menu inside the viewport.
      const rect = menu.getBoundingClientRect();
      const px = Math.min(x, window.innerWidth - rect.width - 4);
      const py = Math.min(y, window.innerHeight - rect.height - 4);
      menu.style.left = Math.max(4, px) + "px";
      menu.style.top = Math.max(4, py) + "px";
      const self = this;
      menu.addEventListener("mousedown", (e) => e.stopPropagation());
      menu.addEventListener("click", (e) => {
        const li = e.target.closest("li");
        if (!li || li.classList.contains("lm-disabled")) return;
        const act = li.dataset.act;
        self._hideContextMenu();
        const ids = self.getSelectedIds();
        if (act === "above" && self.onInsert) self.onInsert(item, "above");
        else if (act === "below" && self.onInsert) self.onInsert(item, "below");
        else if (act === "dup" && self.onBulkDuplicate) self.onBulkDuplicate(ids);
        else if (act === "del" && self.onBulkDelete) self.onBulkDelete(ids);
        else if (act === "up" && self.onMove) self.onMove(ids, "up");
        else if (act === "down" && self.onMove) self.onMove(ids, "down");
      });
      this._menuEl = menu;
    }

    async _commit(td, value, orig, ctl) {
      if (orig == null) orig = "";
      if (value === orig) return;
      const tr = td.closest("tr");
      const id = Number(tr.dataset.id);
      const version = Number(tr.dataset.version);
      const key = td.dataset.key;
      td.classList.add("lm-saving");
      try {
        const updated = await self_save(this, id, version, key, value);
        td.classList.remove("lm-saving", "lm-cell-error");
        td.classList.add("lm-saved");
        setTimeout(() => td.classList.remove("lm-saved"), 800);
        tr.dataset.version = updated.version;
        this._reflect(td, ctl, updated[key]);
      } catch (ex) {
        td.classList.remove("lm-saving");
        td.classList.add("lm-cell-error");
        td.title = ex.message || "保存失败";
        if (ex.code === "VERSION_CONFLICT" && ex.details && ex.details.server_data) {
          tr.dataset.version = ex.details.server_version;
          this._reflect(td, ctl, ex.details.server_data[key]);
          global.LMToast && global.LMToast("该行已被他人修改，已刷新为最新值", false);
        }
      }
    }

    // Push the canonical stored value back into the cell's editor.
    _reflect(td, ctl, stored) {
      const dt = td.dataset.type;
      if (ctl && ctl.multiple) {
        const cur = Array.isArray(stored)
          ? stored.map(String)
          : (stored ? String(stored).split(/[;,]/).map((s) => s.trim()) : []);
        Array.from(ctl.options).forEach((o) => { o.selected = cur.indexOf(o.value) >= 0; });
        ctl.dataset.orig = cur.join(";");
        return;
      }
      if (ctl && ctl.tagName === "SELECT") {
        let v = stored;
        if (dt === "boolean") v = stored === true ? "是" : stored === false ? "否" : "";
        v = v == null ? "" : String(v);
        ctl.value = v;
        ctl.dataset.orig = v;
        return;
      }
      if (ctl) { // date / datetime input
        let v = stored == null ? "" : String(stored);
        if (dt === "datetime" && v.length > 16) v = v.slice(0, 16);
        ctl.value = v;
        ctl.dataset.orig = v;
        return;
      }
      let disp = Array.isArray(stored) ? stored.join("; ") : stored;
      td.textContent = disp == null ? "" : disp;
    }
  }

  async function self_save(grid, id, version, key, value) {
    const changes = {};
    changes[key] = value;
    return grid.onSave({ id, version }, changes);
  }

  function degradeBanner(host, msg) {
    if (!host || host.querySelector(".lm-grid-degrade")) return;
    const b = document.createElement("div");
    b.className = "lm-grid-degrade";
    b.setAttribute("role", "alert");
    b.style.cssText =
      "margin:0 0 8px;padding:8px 12px;border:1px solid #e0a800;" +
      "background:#fff8e1;color:#7a5c00;border-radius:6px;font-size:13px;";
    b.textContent = msg;
    host.parentNode ? host.parentNode.insertBefore(b, host) : host.appendChild(b);
  }

  const LMGrid = {
    /* Univer Sheets is the primary editing engine for the Test Matrix — it gives
     * spreadsheet-grade copy/paste, block paste and batch cell fill. The built-in
     * FallbackGrid is only an emergency degrade used when the vendored Univer
     * bundle is missing (not yet built) or fails to load, so the app is never
     * bricked; a banner then tells the operator to build frontend/. */
    create(opts) {
      if (global.LMUniver && typeof global.LMUniver.mount === "function") {
        try {
          const g = global.LMUniver.mount(opts);
          // The multi-sheet editor drives the grid through setSheetFields /
          // setSheetData. A vendored Univer bundle that predates that contract
          // (stale build) still mounts, but then silently fails to paint the
          // header and rows — "shows the row count but no content". Detect the
          // missing API and degrade to the dependency-free FallbackGrid, which
          // always renders, instead of shipping a blank spreadsheet.
          if (typeof g.setSheetFields !== "function") {
            try { if (typeof g.dispose === "function") g.dispose(); } catch (_e) {}
            if (opts && opts.host) opts.host.innerHTML = "";
            console.warn(
              "Univer bundle is stale (no multi-sheet API); using built-in grid.");
            degradeBanner(opts && opts.host,
              "Univer 表格为旧版本（缺少多 Sheet 接口），已改用内置表格以确保数据正常显示。" +
              "请在 frontend/ 执行 npm install && npm run build 重新构建以恢复 Univer。");
            return new FallbackGrid(opts);
          }
          g.engine = "univer";
          return g;
        } catch (e) {
          console.warn("Univer mount failed, using built-in grid:", e);
          degradeBanner(opts && opts.host,
            "Univer 表格加载失败，已临时使用内置表格（复制粘贴/批量单元格能力受限）。请检查 vendor/univer 构建产物。");
        }
      } else {
        degradeBanner(opts && opts.host,
          "未检测到 Univer 表格引擎，正使用内置表格。请在 frontend/ 执行 npm install && npm run build 以启用复制粘贴/批量单元格。");
      }
      return new FallbackGrid(opts);
    },
    FallbackGrid,
  };

  global.LMGrid = LMGrid;
})(window);
