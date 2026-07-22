/*
 * LMStepsRefPanel — a lightweight Lib/Const reference search panel embedded in
 * the step-detail editor. It lets the author look up Lib functions and Const
 * definitions from the CURRENT project (sourced from the shared Y.Doc, or the
 * DB when collaboration is off) and copy a ready-to-paste token into a cell.
 *
 * Copy formats (per spec):
 *   - lib   -> lib_func                     (goes into the サブルーチン column)
 *   - const -> const_jname(const_name)
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
  function esc(v) {
    return s(v).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // Copy text to the clipboard with a synchronous fallback for browsers/contexts
  // where navigator.clipboard is unavailable (e.g. some http:// LAN origins).
  function copyText(text) {
    try {
      if (global.navigator && navigator.clipboard && navigator.clipboard.writeText) {
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
      ta.select();
      const ok = document.execCommand && document.execCommand("copy");
      document.body.removeChild(ta);
      return !!ok;
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

  // ---- panel ------------------------------------------------------------- //

  function LMStepsRefPanel(root) {
    this.root = root;
    this.tab = "lib";        // "lib" | "const"
    this.query = "";
    this.data = { lib: [], const: [] };  // arrays of entry objects
    this._build();
  }

  LMStepsRefPanel.prototype._build = function () {
    const r = this.root;
    r.innerHTML =
      '<div class="lm-ref-head">' +
      '  <div class="lm-ref-tabs">' +
      '    <button type="button" class="lm-ref-tab is-on" data-tab="lib">Lib 函数</button>' +
      '    <button type="button" class="lm-ref-tab" data-tab="const">Const 常量</button>' +
      '  </div>' +
      '  <input type="search" class="lm-ref-search lm-input" placeholder="搜索名称 / 引数 / 值 / 备注…">' +
      '  <div class="lm-ref-count lm-muted"></div>' +
      '</div>' +
      '<div class="lm-ref-list" tabindex="0"></div>' +
      '<div class="lm-ref-hint lm-muted">点击条目复制，再粘贴到单元格</div>';

    this.searchEl = r.querySelector(".lm-ref-search");
    this.listEl = r.querySelector(".lm-ref-list");
    this.countEl = r.querySelector(".lm-ref-count");
    this.tabEls = Array.prototype.slice.call(r.querySelectorAll(".lm-ref-tab"));

    const self = this;
    this.tabEls.forEach(function (b) {
      b.addEventListener("click", function () { self._setTab(b.getAttribute("data-tab")); });
    });
    this.searchEl.addEventListener("input", function () {
      self.query = self.searchEl.value.trim().toLowerCase();
      self._renderList();
    });
    // Delegate copy clicks (list is re-rendered on every keystroke).
    this.listEl.addEventListener("click", function (ev) {
      const el = ev.target.closest ? ev.target.closest(".lm-ref-item") : null;
      if (!el) return;
      const idx = Number(el.getAttribute("data-idx"));
      self._copyAt(idx, el);
    });
  };

  LMStepsRefPanel.prototype._setTab = function (tab) {
    if (tab !== "lib" && tab !== "const") return;
    this.tab = tab;
    this.tabEls.forEach(function (b) {
      b.classList.toggle("is-on", b.getAttribute("data-tab") === tab);
    });
    this._renderList();
  };

  // Accept raw rows: { lib: [...rows], const: [...rows] }. Rows are mapped to
  // entries and null (missing key field) rows are dropped.
  LMStepsRefPanel.prototype.setData = function (raw) {
    const lib = ((raw && raw.lib) || []).map(libEntry).filter(Boolean);
    const con = ((raw && raw.const) || []).map(constEntry).filter(Boolean);
    // Stable, case-insensitive sort by title for predictable scanning.
    const byTitle = function (a, b) { return a.title.localeCompare(b.title); };
    lib.sort(byTitle); con.sort(byTitle);
    this.data = { lib: lib, const: con };
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
      this.listEl.innerHTML =
        '<div class="lm-ref-empty lm-muted">' +
        (this._current().length ? "无匹配结果" : "该项目暂无数据") + "</div>";
      return;
    }
    const html = list.map(function (e, i) {
      const sub = e.sub ? '<div class="lm-ref-sub">' + esc(e.sub) + "</div>" : "";
      const note = e.note ? '<div class="lm-ref-note">' + esc(e.note) + "</div>" : "";
      return '<div class="lm-ref-item" data-idx="' + i + '" title="点击复制：' +
        esc(e.copy) + '">' +
        '<div class="lm-ref-title">' + esc(e.title) +
        '<span class="lm-ref-copy">复制</span></div>' +
        sub + note + "</div>";
    }).join("");
    this.listEl.innerHTML = html;
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
