/* Explicit row-structure controls for the matrix editor.
 *
 * WHY THIS EXISTS
 * ---------------
 * Row operations (insert / duplicate / delete / move) used to be reachable only
 * from the built-in grid's right-click menu (grid.js `_showContextMenu`). That
 * menu belongs to FallbackGrid, which is the *emergency degrade* path — the
 * primary engine is Univer. Under Univer:
 *
 *   - insert-row / remove-row ARE reachable, because adapter.ts intercepts
 *     Univer's own `sheet.command.insert-row` / `sheet.command.remove-row`
 *     and routes them to onInsert / onBulkDelete;
 *   - move (上移/下移) and duplicate (复制所选) are NOT. adapter.ts declares
 *     `onMove` and `onBulkDuplicate` in its options type and never calls them.
 *
 * So on the default engine those two operations are not "hidden in a context
 * menu" — they have no entry point at all. Explicit toolbar buttons are the
 * only way to reach them, not merely a nicer way.
 *
 * The buttons live in #lm-toolbar-right, which adapter.ts adopts into Univer's
 * own ribbon via registerUIPart(TOOLBAR), so one markup site serves both
 * engines.
 *
 * `plan()` is a pure function: it decides which controls are offered and why
 * one is unavailable, with no DOM and no network. That is the part worth
 * testing, and it is testable in a bare Node process.
 */
(function (global) {
  "use strict";

  // Every control, in toolbar order. `needsSelection` mirrors the guards in
  // editor.js: bulkDuplicate / bulkDelete / moveRows all bail out with
  // "请先勾选…" when the selection is empty, whereas insertRowAt falls back to
  // appending at the end (editor.js `if (!anchor) return addRow();`).
  var ACTIONS = [
    { action: "insert-above", label: "上方插入行", needsSelection: false },
    { action: "insert-below", label: "下方插入行", needsSelection: false },
    { action: "duplicate", label: "复制所选", needsSelection: true },
    { action: "delete", label: "删除所选", needsSelection: true, danger: true },
    { action: "move-up", label: "上移", needsSelection: true },
    { action: "move-down", label: "下移", needsSelection: true },
  ];

  var NEEDS_SELECTION_HINT = "请先勾选要操作的行";
  var LOCKED_HINT = "项目当前不可编辑";

  // Insert with nothing selected is legal but does something different from
  // what the label suggests, so say so rather than letting the user find out.
  var APPEND_HINT = "未选中行时，新行追加到末尾";

  /* Decide which row controls to offer.
   *
   * state: { selected: number, editable: boolean }
   *   selected  how many rows are currently selected
   *   editable  false when the project is locked (the server rejects every
   *             structural write with PROJECT_LOCKED, so offering the buttons
   *             would only produce a toast)
   *
   * Returns [{ action, label, enabled, hint, danger }] — always the full list,
   * so the toolbar keeps a stable shape and buttons grey out instead of
   * appearing and disappearing under the cursor.
   */
  function plan(state) {
    var st = state || {};
    var selected = Number(st.selected) || 0;
    if (selected < 0) selected = 0;
    // Absent `editable` means "not told otherwise" -> assume editable, matching
    // how the rest of the editor treats a missing project flag.
    var editable = st.editable !== false;

    return ACTIONS.map(function (a) {
      var enabled = true;
      var hint = "";
      if (!editable) {
        enabled = false;
        hint = LOCKED_HINT;
      } else if (a.needsSelection && selected === 0) {
        enabled = false;
        hint = NEEDS_SELECTION_HINT;
      } else if (!a.needsSelection && selected === 0) {
        hint = APPEND_HINT;
      }
      var label = a.label;
      // Count in the label for the destructive/bulk actions, so the user can
      // see the blast radius before clicking rather than in the confirm dialog.
      if (a.needsSelection && selected > 0) label = a.label + " (" + selected + ")";
      return {
        action: a.action,
        label: label,
        enabled: enabled,
        hint: hint,
        danger: !!a.danger,
      };
    });
  }

  /* Render (or re-render in place) the plan into `host`.
   * Buttons carry data-act; the caller delegates one click handler on the host.
   */
  function render(host, rows) {
    if (!host) return;
    host.innerHTML = "";
    (rows || []).forEach(function (r) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-sm lm-rowtool" + (r.danger ? " btn-danger" : "");
      b.dataset.act = r.action;
      b.textContent = r.label;
      b.disabled = !r.enabled;
      // A disabled button gets no title tooltip in most browsers, so the reason
      // also goes on the wrapper via aria-description for assistive tech.
      if (r.hint) {
        b.title = r.hint;
        b.setAttribute("aria-description", r.hint);
      }
      host.appendChild(b);
    });
  }

  /* Keyboard shortcuts, for the ? help panel.
   *
   * Only bindings that actually exist in the shipped code are listed. A help
   * panel that documents a shortcut which does nothing is worse than no help
   * panel: it makes the user distrust every other entry. Each line below cites
   * the code that implements it.
   */
  var SHORTCUTS = [
    { keys: ["Ctrl", "S"], mac: ["⌘", "S"], scope: "编辑器",
      desc: "强制保存当前单元格，并拉取他人的最新修改" },   // editor.js installForceSaveShortcut
    { keys: ["Enter"], scope: "单元格",
      desc: "提交当前单元格的编辑" },                        // grid.js cell keydown
    { keys: ["Esc"], scope: "单元格",
      desc: "放弃当前单元格的编辑，恢复原值" },              // grid.js cell keydown
    { keys: ["Esc"], scope: "对话框",
      desc: "关闭右键菜单、步骤明细等弹出层" },              // grid.js / steps_editor.js
    { keys: ["Tab"], scope: "JSON 编辑",
      desc: "在 JSON 编辑框内插入缩进，而不是跳到下一个控件" }, // json_editor.js
    { keys: ["?"], scope: "全局", desc: "打开 / 关闭本快捷键面板" },
  ];

  // Mouse/other interactions that are not key bindings but are equally
  // undiscoverable. Kept separate so the panel can label them honestly.
  var GESTURES = [
    { what: "右键点击行", desc: "打开行操作菜单（仅内置表格引擎可用）" },
    { what: "工具栏行操作按钮", desc: "插入 / 复制 / 删除 / 上移 / 下移，两种表格引擎均可用" },
  ];

  function isMac() {
    var p = (global.navigator && (global.navigator.platform ||
      global.navigator.userAgent)) || "";
    return /Mac|iPhone|iPad/i.test(p);
  }

  /* Resolve each shortcut's key list for the current platform. */
  function shortcutRows(mac) {
    var onMac = mac === undefined ? isMac() : !!mac;
    return SHORTCUTS.map(function (s) {
      return {
        keys: (onMac && s.mac ? s.mac : s.keys).slice(),
        scope: s.scope,
        desc: s.desc,
      };
    });
  }

  /* Should this keydown open the help panel?
   *
   * "?" must not fire while the user is typing — the editor is a grid full of
   * text inputs and contenteditable cells, where "?" is an ordinary character.
   * Modifier combos are excluded too so Ctrl+? / ⌘+? stay available to the
   * browser. `target` is the event target; the caller passes e.target.
   */
  function isHelpKey(e) {
    if (!e) return false;
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    if (e.key !== "?") return false;
    return !isTypingTarget(e.target);
  }

  function isTypingTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select";
  }

  global.LMRowTools = {
    ACTIONS: ACTIONS,
    SHORTCUTS: SHORTCUTS,
    GESTURES: GESTURES,
    NEEDS_SELECTION_HINT: NEEDS_SELECTION_HINT,
    LOCKED_HINT: LOCKED_HINT,
    APPEND_HINT: APPEND_HINT,
    plan: plan,
    render: render,
    shortcutRows: shortcutRows,
    isHelpKey: isHelpKey,
    isTypingTarget: isTypingTarget,
  };
})(window);
