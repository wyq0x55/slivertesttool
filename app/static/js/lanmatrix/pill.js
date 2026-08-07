/* pill.js — the one status-badge vocabulary.
 *
 * Before this file the codebase carried five badge dialects. Three of them
 * (`pill`+`st-*`, `.status`+`.dot.<state>`, `lm-badge`+`lm-status-*`) rendered
 * the *same* task status three different ways, and the `pill()` helper plus its
 * `STATUS_ZH` map were copy-pasted verbatim into home.js, admin_console.js and
 * project_tasks.js. steps_editor.js had neither, so the steps dialog showed the
 * raw English `passed` next to a list showing `通过`.
 *
 * `pill` + `st-*` won because it was already the majority and it is the only
 * one that encodes status as shape *and* colour (the `.dot` child), so the
 * state survives a colour-blind reader and a greyscale screenshot.
 *
 * Two rendering paths are exposed because the call sites genuinely differ:
 *   - `html()` for the innerHTML template builders (table rows),
 *   - `apply()` for the three places that mutate a long-lived element in place.
 * `apply()` builds DOM nodes instead of assigning innerHTML: the label can come
 * from a server-supplied status string, and this keeps that path free of any
 * markup-injection question.
 *
 * `.status`+`.dot.<state>` on the project cards is deliberately NOT folded in
 * here — a card wants a light haloed dot, not a filled chip.
 */
(function (window, document) {
  "use strict";

  /* Execution lifecycle of a test task. `notask` = never queued. */
  var TASK_ZH = {
    queued: "排队中", running: "运行中", passed: "通过", failed: "失败",
    error: "异常", cancelled: "已取消", notask: "—"
  };

  /* Project lifecycle. Kept separate from TASK_ZH rather than merged: both
   * vocabularies contain `active`-like states and a single flat map would let a
   * project status silently pick up a task label. */
  var PROJECT_ZH = {
    draft: "草稿", active: "进行中", frozen: "冻结", archived: "归档"
  };

  /* Boolean-ish flags rendered as pills (必填 / 启用). */
  var FLAG_ZH = { on: "启用", off: "停用" };

  var ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ESCAPES[c];
    });
  }

  /* Normalise a raw status into a class suffix. Empty/null means "no task has
   * ever been queued for this row", which is a real state with its own label,
   * not a missing value to be hidden. */
  function norm(status) {
    var s = String(status == null ? "" : status).trim().toLowerCase();
    return s || "notask";
  }

  /* Resolve the visible label. An explicit `label` always wins so callers that
   * merge status+verdict (a `failed` run whose verdict is ERROR) keep control. */
  function labelFor(c, label, dict) {
    if (label != null && label !== "") return String(label);
    var d = dict || TASK_ZH;
    return d[c] || PROJECT_ZH[c] || FLAG_ZH[c] || c;
  }

  function html(status, label, tip) {
    var c = norm(status);
    var text = labelFor(c, label);
    return '<span class="pill st-' + esc(c) + '" title="' + esc(tip || text) +
      '"><span class="dot"></span>' + esc(text) + "</span>";
  }

  /* In-place variant for elements that persist across renders. Rebuilds the
   * children every call so a status change can never leave the previous dot or
   * a stale text node behind. */
  function apply(el, status, label, tip) {
    if (!el) return;
    var c = norm(status);
    var text = labelFor(c, label);
    el.className = "pill st-" + c;
    el.title = tip || text;
    while (el.firstChild) el.removeChild(el.firstChild);
    var dot = document.createElement("span");
    dot.className = "dot";
    el.appendChild(dot);
    el.appendChild(document.createTextNode(text));
  }

  window.LMPill = {
    TASK_ZH: TASK_ZH,
    PROJECT_ZH: PROJECT_ZH,
    FLAG_ZH: FLAG_ZH,
    esc: esc,
    norm: norm,
    html: html,
    apply: apply
  };
})(window, document);
