/*
 * LMStepsRefPanel — a lightweight Lib/Const reference search panel embedded in
 * the step-detail editor. It lets the author look up Lib functions and Const
 * definitions from the CURRENT project (sourced from the shared Y.Doc, or the
 * DB when collaboration is off) and copy a ready-to-paste token into a cell.
 *
 * Copy formats (per spec):
 *   - lib   -> lib_func                     (goes into the サブルーチン column)
 *   - const -> const_jname(const_name)
 *   - io    -> io_name(io_path)             (single 入力/期待 signal cell)
 *
 * The const / io tabs also expose an inline "+ 新增" form that adds a new entry
 * to the project's pool through a host-supplied ``onAdd(kind, values)`` hook
 * (uniqueness — io: name & path; const: identifier — is enforced by the host).
 *
 * The panel is data-source agnostic: the host hands it plain row objects whose
 * fields are already flattened (collab getItems() = Y.Map.toJSON(); REST
 * to_dict() = data.update(custom_values)). No engine coupling.
 */
(function (global) {
  "use strict";

  function s(v) {
    return v === null || v === undefined ? "" : String(v);
  }
  function makeEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // Copy text to the clipboard with a synchronous fallback for browsers/contexts
  // where navigator.clipboard is unavailable (e.g. some http:// LAN origins).
  function copyText(text) {
    try {
      if (global.navigator && navigator.clipboard && global.isSecureContext && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).then(function () { return true; },
          function () { return legacyCopy(text); });
      }
    } catch (_e) { /* fall through */ }
    return Promise.resolve(legacyCopy(text));
  }
  function legacyCopy(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      document.body.appendChild(ta);
      try {
        ta.select();
        return !!(document.execCommand && document.execCommand("copy"));
      } finally {
        ta.remove();
      }
    } catch (_e) { return false; }
  }

  // ---- entry models ------------------------------------------------------ //

  function libEntry(row) {
    const func = s(row.lib_func).trim();
    if (!func) return null;
    return {
      copy: func,
      title: func,
      sub: s(row.lib_arg).trim(),
      note: s(row.lib_note).trim(),
      hay: (func + " " + s(row.lib_arg) + " " + s(row.lib_note) +
        " " + s(row.lib_name)).toLowerCase(),
    };
  }

  function constEntry(row) {
    const name = s(row.const_name).trim();
    const jname = s(row.const_jname).trim();
    if (!name && !jname) return null;
    const copy = jname ? (jname + "(" + name + ")") : name;
    return {
      copy: copy,
      title: jname || name,
      sub: name && jname ? name : s(row.const_value).trim(),
      note: s(row.const_note).trim(),
      value: s(row.const_value).trim(),
      hay: (name + " " + jname + " " + s(row.const_value) + " " +
        s(row.const_note)).toLowerCase(),
    };
  }

  function ioEntry(row) {
    const name = s(row.io_name).trim();
    const path = s(row.io_path).trim();
    if (!name && !path) return null;
    const copy = path ? (name + "(" + path + ")") : name;
    return {
      copy: copy,
      title: name || path,
      sub: path,
      note: s(row.io_note).trim(),
      hay: (name + " " + path + " " + s(row.io_note)).toLowerCase(),
    };
  }

  // Per-tab "+ 新增" form field spec (lib has no add form).
  const ADD_FORMS = {
    const: [
      { key: "const_name", label: "识别子名", required: true },
      { key: "const_jname", label: "和名" },
      { key: "const_value", label: "值" },
    ],
    io: [
      { key: "io_name", label: "名称", required: true },
      { key: "io_path", label: "路径" },
      { key: "io_note", label: "备考" },
    ],
  };

  // ---- panel ------------------------------------------------------------- //

  function LMStepsRefPanel(root) {
    this.root = root;
    this.tab = "lib";        // "lib" | "const" | "io"
    this.query = "";
    this.data = { lib: [], const: [], io: [] };  // arrays of entry objects
    this.onAdd = null;       // host hook: (kind, values) => Promise
    this.canAdd = true;      // whether the "+ 新增" affordance is shown
    this._build();
  }

  LMStepsRefPanel.prototype._build = function () {
    const r = this.root;
    const self = this;

    const tabsEl = makeEl("div", "lm-ref-tabs");
    const tabSpecs = [
      { tab: "lib", label: "Lib 函数", on: true },
      { tab: "const", label: "Const 常量", on: false },
      { tab: "io", label: "入出力", on: false },
    ];
    this.tabEls = tabSpecs.map(function (t) {
      const btn = makeEl("button", "lm-ref-tab" + (t.on ? " is-on" : ""), t.label);
      btn.type = "button";
      btn.setAttribute("data-tab", t.tab);
      btn.addEventListener("click", function () { self._setTab(t.tab); });
      tabsEl.appendChild(btn);
      return btn;
    });

    this.searchEl = makeEl("input", "lm-ref-search lm-input");
    this.searchEl.type = "search";
    this.searchEl.placeholder = "搜索名称 / 路径 / 值 / 备注…";
    this.searchEl.setAttribute("aria-label", "搜索名称 / 路径 / 值 / 备注…");

    this.countEl = makeEl("div", "lm-ref-count lm-muted");
    this.addBtn = makeEl("button", "lm-ref-addbtn btn btn-sm", "+ 新增");
    this.addBtn.type = "button";
    this.addBtn.hidden = true;
    const toolbarEl = makeEl("div", "lm-ref-toolbar");
    toolbarEl.append(this.countEl, this.addBtn);

    this.addForm = makeEl("div", "lm-ref-add");
    this.addForm.hidden = true;

    const headEl = makeEl("div", "lm-ref-head");
    headEl.append(tabsEl, this.searchEl, toolbarEl, this.addForm);

    this.listEl = makeEl("div", "lm-ref-list");
    this.listEl.tabIndex = 0;

    const hintEl = makeEl("div", "lm-ref-hint lm-muted", "点击条目复制，再粘贴到单元格");
    r.replaceChildren(headEl, this.listEl, hintEl);

    this.searchEl.addEventListener("input", function () {
      self.query = self.searchEl.value.trim().toLowerCase();
      self._renderList();
    });
    // Guard the "+ 新增" affordances: they're optional, so a missing node must
    // never throw and take down the whole panel (which would silently disable
    // the 参考 button). .lm-ref-add is a <div>, not a <form>: the panel's root
    // (#lm-steps-ref) already lives inside the dialog's <form>, and HTML forbids
    // nesting a <form> in a <form> — the parser silently drops the inner one, so
    // querySelector would return null. We use a plain container and submit via
    // the save button (see _showAdd) plus Enter-to-submit here.
    if (this.addBtn) {
      this.addBtn.addEventListener("click", function () { self._toggleAdd(); });
    }
    if (this.addForm) {
      this.addForm.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          if (ev.isComposing || ev.keyCode === 229) return;
          ev.preventDefault();
          self._submitAdd();
        }
      });
    }
    // Delegate copy clicks (list is re-rendered on every keystroke).
    this.listEl.addEventListener("click", function (ev) {
      const el = ev.target.closest ? ev.target.closest(".lm-ref-item") : null;
      if (!el) return;
      const idx = Number(el.getAttribute("data-idx"));
      self._copyAt(idx, el);
    });
  };

  LMStepsRefPanel.prototype._setTab = function (tab) {
    if (tab !== "lib" && tab !== "const" && tab !== "io") return;
    this.tab = tab;
    this.tabEls.forEach(function (b) {
      b.classList.toggle("is-on", b.getAttribute("data-tab") === tab);
    });
    // "+ 新增" only applies to the managed pools (const / io) and only when the
    // host wired an onAdd hook.
    const canAdd = this.canAdd && typeof this.onAdd === "function" && !!ADD_FORMS[tab];
    if (this.addBtn) this.addBtn.hidden = !canAdd;
    this._hideAdd();
    this._renderList();
  };

  // ---- "+ 新增" inline form --------------------------------------------- //

  LMStepsRefPanel.prototype._toggleAdd = function () {
    if (!this.addForm) return;
    if (this.addForm.hidden) this._showAdd(); else this._hideAdd();
  };

  LMStepsRefPanel.prototype._hideAdd = function () {
    if (!this.addForm) return;
    this.addForm.hidden = true;
    this.addForm.replaceChildren();
    if (this.addBtn) this.addBtn.classList.remove("is-on");
  };

  LMStepsRefPanel.prototype._showAdd = function () {
    if (!this.addForm) return;
    const spec = ADD_FORMS[this.tab];
    if (!spec) return;
    const self = this;
    let firstInput = null;
    const fieldNodes = spec.map(function (f) {
      const row = makeEl("label", "lm-ref-addrow");
      const labelEl = makeEl("span", "", f.label + (f.required ? " *" : ""));
      const inputEl = makeEl("input", "lm-input");
      inputEl.setAttribute("data-key", f.key);
      if (f.required) inputEl.required = true;
      if (!firstInput) firstInput = inputEl;
      row.append(labelEl, inputEl);
      return row;
    });
    const errEl = makeEl("div", "lm-ref-adderr lm-err");
    errEl.hidden = true;
    const saveBtn = makeEl("button", "btn btn-sm btn-primary lm-ref-addsave", "保存");
    saveBtn.type = "button";
    saveBtn.addEventListener("click", function () { self._submitAdd(); });
    const cancelBtn = makeEl("button", "btn btn-sm lm-ref-addcancel", "取消");
    cancelBtn.type = "button";
    cancelBtn.addEventListener("click", function () { self._hideAdd(); });
    const actionsEl = makeEl("div", "lm-ref-addactions");
    actionsEl.append(saveBtn, cancelBtn);

    this.addForm.replaceChildren(...fieldNodes, errEl, actionsEl);
    this.addForm.hidden = false;
    if (this.addBtn) this.addBtn.classList.add("is-on");
    if (firstInput) { try { firstInput.focus(); } catch (_e) { /* noop */ } }
  };

  LMStepsRefPanel.prototype._submitAdd = function () {
    if (!this.addForm || typeof this.onAdd !== "function") return;
    const kind = this.tab;
    const spec = ADD_FORMS[kind];
    if (!spec) return;
    const values = {};
    Array.prototype.forEach.call(this.addForm.querySelectorAll("input[data-key]"),
      function (el) { values[el.getAttribute("data-key")] = el.value.trim(); });
    const errEl = this.addForm.querySelector(".lm-ref-adderr");
    const saveBtn = this.addForm.querySelector(".lm-ref-addsave");
    const missing = spec.filter(function (f) { return f.required && !values[f.key]; });
    if (missing.length) {
      if (errEl) { errEl.textContent = missing[0].label + "不能为空"; errEl.hidden = false; }
      return;
    }
    if (errEl) errEl.hidden = true;
    if (saveBtn) saveBtn.disabled = true;
    const self = this;
    Promise.resolve()
      .then(function () { return self.onAdd(kind, values); })
      .then(function () { self._hideAdd(); })
      .catch(function (ex) {
        if (errEl) { errEl.textContent = (ex && ex.message) || "新增失败"; errEl.hidden = false; }
      })
      .then(function () { if (saveBtn) saveBtn.disabled = false; });
  };

  // Accept raw rows: { lib: [...rows], const: [...rows], io: [...rows] }. Rows
  // are mapped to entries and null (missing key field) rows are dropped.
  LMStepsRefPanel.prototype.setData = function (raw) {
    // Stable, case-insensitive sort by title for predictable scanning.
    const byTitle = function (a, b) { return a.title.localeCompare(b.title); };
    const lib = ((raw && raw.lib) || []).map(libEntry).filter(Boolean).toSorted(byTitle);
    const con = ((raw && raw.const) || []).map(constEntry).filter(Boolean).toSorted(byTitle);
    const io = ((raw && raw.io) || []).map(ioEntry).filter(Boolean).toSorted(byTitle);
    this.data = { lib: lib, const: con, io: io };
    this._renderList();
  };

  LMStepsRefPanel.prototype._current = function () {
    return this.data[this.tab] || [];
  };

  LMStepsRefPanel.prototype._filtered = function () {
    const all = this._current();
    if (!this.query) return all;
    const q = this.query;
    return all.filter(function (e) { return e.hay.indexOf(q) >= 0; });
  };

  LMStepsRefPanel.prototype._renderList = function () {
    const list = this._filtered();
    this._visible = list;
    this.countEl.textContent = list.length + " / " + this._current().length;
    if (!list.length) {
      this.listEl.replaceChildren(makeEl(
        "div",
        "lm-ref-empty lm-muted",
        this._current().length ? "无匹配结果" : "该项目暂无数据"
      ));
      return;
    }
    const items = list.map(function (e, i) {
      const itemEl = makeEl("div", "lm-ref-item");
      itemEl.setAttribute("data-idx", String(i));
      itemEl.title = "点击复制：" + e.copy;

      const titleEl = makeEl("div", "lm-ref-title", e.title);
      titleEl.appendChild(makeEl("span", "lm-ref-copy", "复制"));
      itemEl.appendChild(titleEl);

      if (e.sub) itemEl.appendChild(makeEl("div", "lm-ref-sub", e.sub));
      if (e.note) itemEl.appendChild(makeEl("div", "lm-ref-note", e.note));
      return itemEl;
    });
    this.listEl.replaceChildren(...items);
  };

  LMStepsRefPanel.prototype._copyAt = function (idx, el) {
    const list = this._visible || [];
    const e = list[idx];
    if (!e) return;
    const self = this;
    copyText(e.copy).then(function (ok) {
      el.classList.add(ok ? "is-copied" : "is-copyfail");
      const tag = el.querySelector(".lm-ref-copy");
      if (tag) tag.textContent = ok ? "已复制✓" : "复制失败";
      setTimeout(function () {
        el.classList.remove("is-copied", "is-copyfail");
        if (tag) tag.textContent = "复制";
      }, 1200);
    });
  };

  // Focus the search box (called when the panel is opened/shown).
  LMStepsRefPanel.prototype.focusSearch = function () {
    try { this.searchEl.focus(); this.searchEl.select(); } catch (_e) { /* noop */ }
  };

  global.LMStepsRefPanel = LMStepsRefPanel;
})(window);
