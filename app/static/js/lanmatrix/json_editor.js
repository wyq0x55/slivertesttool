/* LMCodeEditor — a tiny, dependency-free CodeMirror-style JSON code editor.
 *
 * The project is built and served fully offline (no npm install / no CDN at
 * runtime), so the npm `codemirror` package cannot be bundled here. This module
 * provides the equivalent editing surface with zero external dependencies:
 *
 *   - a line-number gutter,
 *   - JSON syntax highlighting (keys / strings / numbers / keywords / punct),
 *   - live parse validation with an error indicator,
 *   - a `change` event and imperative get/set value API.
 *
 * Implementation: a transparent <textarea> (the real editing surface, owns the
 * caret, selection, undo and scrolling) overlaid on a syntax-highlighted <pre>
 * that mirrors the text; a gutter tracks the textarea scroll. Both layers share
 * identical font metrics so the highlight lines up exactly under the caret.
 */
(function (global) {
  "use strict";

  function tokenSpan(cls, text) {
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = text;
    return span;
  }

  // Tokenise a JSON string into highlighted DOM nodes. Never throws: on any
  // input it degrades to plain text nodes, so a half-typed document still renders.
  const TOKEN_RE =
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|([{}\[\],:])/g;

  function highlight(src) {
    const nodes = [];
    let last = 0;
    let m;
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(src))) {
      if (m.index > last) nodes.push(document.createTextNode(src.slice(last, m.index)));
      if (m[1] != null) {
        if (m[2] != null) {
          // A quoted string immediately followed by ":" is an object key.
          nodes.push(tokenSpan("lm-ce-key", m[1]), tokenSpan("lm-ce-punct", m[2]));
        } else {
          nodes.push(tokenSpan("lm-ce-str", m[1]));
        }
      } else if (m[3] != null) {
        nodes.push(tokenSpan("lm-ce-kw", m[3]));
      } else if (m[4] != null) {
        nodes.push(tokenSpan("lm-ce-num", m[4]));
      } else if (m[5] != null) {
        nodes.push(tokenSpan("lm-ce-punct", m[5]));
      }
      last = TOKEN_RE.lastIndex;
    }
    if (last < src.length) nodes.push(document.createTextNode(src.slice(last)));
    return nodes;
  }

  class LMCodeEditor {
    constructor(host, opts) {
      opts = opts || {};
      this.host = host;
      this._changeCb = typeof opts.onChange === "function" ? opts.onChange : null;
      this._silent = false;
      this._value = "";
      this._build();
    }

    _build() {
      this.host.classList.add("lm-ce");
      this.host.replaceChildren();

      this.gutter = document.createElement("div");
      this.gutter.className = "lm-ce-gutter";
      this.gutterInner = document.createElement("div");
      this.gutterInner.className = "lm-ce-gutter-inner";
      this.gutter.appendChild(this.gutterInner);

      this.scroll = document.createElement("div");
      this.scroll.className = "lm-ce-scroll";

      this.pre = document.createElement("pre");
      this.pre.className = "lm-ce-hl";
      this.pre.setAttribute("aria-hidden", "true");

      this.ta = document.createElement("textarea");
      this.ta.className = "lm-ce-ta";
      this.ta.spellcheck = false;
      this.ta.autocapitalize = "off";
      this.ta.setAttribute("autocomplete", "off");
      this.ta.setAttribute("autocorrect", "off");
      this.ta.wrap = "off";

      this.scroll.appendChild(this.pre);
      this.scroll.appendChild(this.ta);
      this.host.appendChild(this.gutter);
      this.host.appendChild(this.scroll);

      const onInput = () => {
        this._value = this.ta.value;
        this._render();
        if (!this._silent && this._changeCb) this._changeCb(this._value);
      };
      this.ta.addEventListener("input", onInput);
      // Keep the highlight + gutter glued to the textarea while scrolling.
      this.ta.addEventListener("scroll", () => this._syncScroll());
      // Insert two spaces on an unmodified Tab instead of leaving the field.
      // Modified Tabs must fall through to the browser: Shift+Tab is the way
      // back out of the textarea, and swallowing it would trap the keyboard
      // user inside the field (WCAG 2.1.2 No Keyboard Trap).
      this.ta.addEventListener("keydown", (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
          e.preventDefault();
          this._insertAtCaret("  ");
          this._value = this.ta.value;
          this._render();
          if (!this._silent && this._changeCb) this._changeCb(this._value);
        }
      });
      this._render();
    }

    _insertAtCaret(text) {
      const ta = this.ta;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
      ta.selectionStart = ta.selectionEnd = start + text.length;
    }

    _render() {
      const src = this._value;
      const nodes = highlight(src);
      // Trailing newline needs a filler char so the <pre> keeps the blank last
      // line's height and stays aligned with the textarea.
      if (src.endsWith("\n")) nodes.push(document.createTextNode("\u200b"));
      this.pre.replaceChildren(...nodes);
      const lines = src.length ? src.split("\n").length : 1;
      let g = "";
      for (let i = 1; i <= lines; i++) g += i + "\n";
      this.gutterInner.textContent = g;
      this._syncScroll();
    }

    _syncScroll() {
      const top = this.ta.scrollTop;
      const left = this.ta.scrollLeft;
      this.pre.scrollTop = top;
      this.pre.scrollLeft = left;
      this.gutterInner.style.transform = "translateY(" + (-top) + "px)";
    }

    // --- public API -------------------------------------------------------- //
    setValue(text, silent) {
      const next = text == null ? "" : String(text);
      if (next === this._value) return;
      this._silent = !!silent;
      this._value = next;
      this.ta.value = next;
      this._render();
      this._silent = false;
    }

    getValue() {
      return this.ta.value;
    }

    // Toggle the invalid-JSON indicator and (optionally) surface a message.
    setError(msg) {
      const has = !!msg;
      this.host.classList.toggle("lm-ce-invalid", has);
      if (this._errCb) this._errCb(msg || "");
    }

    onError(cb) { this._errCb = typeof cb === "function" ? cb : null; }

    focus() { try { this.ta.focus(); } catch (_e) { /* noop */ } }

    isFocused() {
      return document.activeElement === this.ta;
    }

    dispose() {
      try { this.host.replaceChildren(); } catch (_e) { /* noop */ }
      this.host.classList.remove("lm-ce", "lm-ce-invalid");
    }
  }

  global.LMCodeEditor = LMCodeEditor;
})(window);
