/*
 * ui.js — shared modal primitives (LMUI.confirm / LMUI.prompt / LMUI.alert).
 *
 * WHY
 * ---
 * The console previously used the browser's native confirm()/prompt() in 19
 * places. That was three separate problems:
 *
 *   1. Destructive and harmless actions looked IDENTICAL. "移除该成员" and
 *      "删除项目及其全部数据" rendered the same grey OS box, so the UI gave the
 *      user no signal about blast radius — the single biggest safety gap in an
 *      enterprise test console.
 *   2. Native dialogs cannot be styled, so they broke the visual language
 *      (and ignore the in-app dark theme entirely).
 *   3. They block the JS thread, and Chrome suppresses them outright after a
 *      few in a row ("阻止此页面创建更多对话框"), which silently turns a
 *      confirmation into an auto-YES. For a delete action that is data loss.
 *
 * DESIGN
 * ------
 * Three levels, so the dialog's weight matches the consequence:
 *   normal   — reversible / low impact  (neutral primary button)
 *   danger   — destructive but scoped   (red button, cancel focused by default)
 *   critical — irreversible + wide blast radius. Adds a type-to-confirm field:
 *              the confirm button stays disabled until the user types the exact
 *              object name. This is the Jira/GitHub pattern for "delete repo".
 *
 * Everything returns a Promise, so call sites read as:
 *     if (!(await LMUI.confirm({ ... }))) return;
 *
 * Accessibility: native <dialog>.showModal() gives us the top layer, a real
 * focus trap, inert background and Esc-to-close for free. We add
 * role/aria-labelledby/aria-describedby, focus restoration, and an explicit
 * Esc->cancel resolution.
 *
 * Graceful degradation: if <dialog> is unsupported we fall back to native
 * confirm()/prompt() rather than leaving the user with a dead button.
 */
(function (window, document) {
  "use strict";

  var SUPPORTS_DIALOG =
    typeof window.HTMLDialogElement === "function" &&
    typeof document.createElement("dialog").showModal === "function";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* Preserve author line breaks without allowing markup through. */
  function richText(s) {
    return esc(s).replace(/\n/g, "<br>");
  }

  var uid = 0;
  function nextId(p) {
    uid += 1;
    return p + "-" + uid;
  }

  /**
   * Core dialog builder shared by confirm/prompt/alert.
   *
   * @param {object} o
   * @param {string} o.title        Heading. Should name the object being acted on.
   * @param {string} [o.body]       Supporting copy; explains consequences.
   * @param {"normal"|"danger"|"critical"} [o.level="normal"]
   * @param {string} [o.confirmText]
   * @param {string} [o.cancelText]
   * @param {string} [o.requireText] critical only: text the user must retype.
   * @param {object} [o.input]      prompt only: {label, value, placeholder, multiline}
   * @param {boolean} [o.alert]     alert mode: single dismiss button.
   * @returns {Promise<boolean|string|null>}
   */
  function open(o) {
    o = o || {};
    var level = o.level || "normal";
    var isCritical = level === "critical" && !!o.requireText;
    var hasInput = !!o.input;

    var titleId = nextId("lmui-t");
    var descId = nextId("lmui-d");
    var lastFocused = document.activeElement;

    var dlg = document.createElement("dialog");
    dlg.className = "lmui lmui--" + level;
    dlg.setAttribute("aria-labelledby", titleId);

    var parts = [];
    parts.push('<form method="dialog" class="lmui-form">');
    parts.push('<div class="lmui-head">');
    if (level !== "normal") {
      // Decorative: the text already states the consequence.
      parts.push(
        '<span class="lmui-icon" aria-hidden="true">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
          'stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>' +
          '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>' +
          "</svg></span>"
      );
    }
    parts.push('<h2 class="lmui-title" id="' + titleId + '">' + esc(o.title || "确认操作") + "</h2>");
    parts.push("</div>");

    if (o.body) {
      parts.push('<p class="lmui-body" id="' + descId + '">' + richText(o.body) + "</p>");
      dlg.setAttribute("aria-describedby", descId);
    }

    if (hasInput) {
      var inId = nextId("lmui-i");
      parts.push('<label class="lmui-label" for="' + inId + '">' + esc(o.input.label || "") + "</label>");
      if (o.input.multiline) {
        parts.push(
          '<textarea class="lmui-input" id="' + inId + '" rows="4" placeholder="' +
            esc(o.input.placeholder || "") + '">' + esc(o.input.value || "") + "</textarea>"
        );
      } else {
        parts.push(
          '<input class="lmui-input" id="' + inId + '" type="text" value="' +
            esc(o.input.value || "") + '" placeholder="' + esc(o.input.placeholder || "") + '">'
        );
      }
    }

    if (isCritical) {
      var reqId = nextId("lmui-r");
      parts.push(
        '<label class="lmui-label" for="' + reqId + '">请输入 <code>' + esc(o.requireText) +
          "</code> 以确认</label>"
      );
      parts.push(
        '<input class="lmui-input lmui-require" id="' + reqId + '" type="text" ' +
          'autocomplete="off" spellcheck="false" placeholder="' + esc(o.requireText) + '">'
      );
    }

    parts.push('<menu class="lmui-foot">');
    if (!o.alert) {
      parts.push(
        '<button type="button" class="btn lmui-cancel" value="cancel">' +
          esc(o.cancelText || "取消") + "</button>"
      );
    }
    var confirmCls = level === "normal" ? "btn primary" : "btn lmui-danger";
    parts.push(
      '<button type="button" class="' + confirmCls + ' lmui-ok">' +
        esc(o.confirmText || (o.alert ? "知道了" : "确定")) + "</button>"
    );
    parts.push("</menu></form>");
    dlg.innerHTML = parts.join("");
    document.body.appendChild(dlg);

    var okBtn = dlg.querySelector(".lmui-ok");
    var cancelBtn = dlg.querySelector(".lmui-cancel");
    var reqInput = dlg.querySelector(".lmui-require");
    var valInput = dlg.querySelector(".lmui-input:not(.lmui-require)");

    /*
     * Several pages bind their own document-level Escape handlers (the SBS
     * editor closes its modal, grid.js hides its menu). A <dialog> is in the
     * top layer, but keydown still BUBBLES to document, so pressing Esc to
     * dismiss this dialog would also fire those handlers and dismiss the thing
     * underneath. Swallow Esc at the capture phase for as long as we are open.
     * stopPropagation only blocks listener dispatch — the UA still performs its
     * default action, so the dialog's own `cancel` event fires as normal.
     */
    function trapEscape(e) {
      if (e.key === "Escape") e.stopPropagation();
    }
    document.addEventListener("keydown", trapEscape, true);

    return new Promise(function (resolve) {
      var settled = false;

      function done(result) {
        if (settled) return;
        settled = true;
        document.removeEventListener("keydown", trapEscape, true);
        // Read the value BEFORE the node is removed from the DOM.
        dlg.close();
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        // Return focus to whatever triggered the dialog so keyboard users are
        // not dumped back at the top of the document.
        try {
          if (lastFocused && lastFocused.focus) lastFocused.focus();
        } catch (e) {
          /* element may be gone (e.g. the row we just deleted) */
        }
        resolve(result);
      }

      function accept() {
        if (okBtn.disabled) return;
        if (hasInput) return done(valInput ? valInput.value : "");
        done(true);
      }
      function reject() {
        done(o.alert ? true : hasInput ? null : false);
      }

      okBtn.addEventListener("click", accept);
      if (cancelBtn) cancelBtn.addEventListener("click", reject);

      // Native Esc (and any other close path) must resolve, never hang.
      dlg.addEventListener("cancel", function (e) {
        e.preventDefault();
        reject();
      });
      dlg.addEventListener("close", function () {
        if (!settled) reject();
      });
      // Click on the backdrop = cancel, matching the rest of the console.
      dlg.addEventListener("click", function (e) {
        if (e.target === dlg) reject();
      });

      if (isCritical) {
        okBtn.disabled = true;
        reqInput.addEventListener("input", function () {
          okBtn.disabled = reqInput.value.trim() !== o.requireText;
        });
        reqInput.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { e.preventDefault(); accept(); }
        });
      }
      if (hasInput && !o.input.multiline) {
        valInput.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { e.preventDefault(); accept(); }
        });
      }

      dlg.showModal();

      // Focus policy: put the caret where the user must act, but for a
      // destructive confirm default to CANCEL so a stray Enter is not a delete.
      if (isCritical) reqInput.focus();
      else if (hasInput) { valInput.focus(); valInput.select(); }
      else if (level === "normal" || o.alert) okBtn.focus();
      else if (cancelBtn) cancelBtn.focus();
    });
  }

  var LMUI = {
    /** @returns {Promise<boolean>} */
    confirm: function (opts) {
      if (typeof opts === "string") opts = { title: opts };
      if (!SUPPORTS_DIALOG) {
        var t = (opts.title || "") + (opts.body ? "\n\n" + opts.body : "");
        return Promise.resolve(window.confirm(t));
      }
      return open(opts);
    },

    /** @returns {Promise<string|null>} null when cancelled. */
    prompt: function (opts) {
      if (typeof opts === "string") opts = { title: opts };
      if (!SUPPORTS_DIALOG) {
        return Promise.resolve(window.prompt(opts.title || "", (opts.input && opts.input.value) || ""));
      }
      opts.input = opts.input || {};
      return open(opts);
    },

    /** @returns {Promise<true>} */
    alert: function (opts) {
      if (typeof opts === "string") opts = { title: opts };
      if (!SUPPORTS_DIALOG) {
        window.alert((opts.title || "") + (opts.body ? "\n\n" + opts.body : ""));
        return Promise.resolve(true);
      }
      opts.alert = true;
      return open(opts);
    },

    /* Transient feedback strip (#lm-toast in base_lm.html).
     *
     * Lives here rather than in a page script because several pages need it and
     * the ones that lacked it (the workspace, the review queue) were failing
     * silently: their toast() was a no-op, so "已通过" and error messages alike
     * went nowhere and the user could not tell whether the click had worked.
     * No-ops when the host element is absent, so it is safe to call anywhere.
     */
    toast: function (msg, ok) {
      var el = document.getElementById("lm-toast");
      if (!el) return;
      el.textContent = String(msg == null ? "" : msg);
      el.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
      el.hidden = false;
      if (el._lmTimer) clearTimeout(el._lmTimer);
      el._lmTimer = setTimeout(function () { el.hidden = true; }, 3200);
    },
  };

  window.LMUI = LMUI;
})(window, document);
