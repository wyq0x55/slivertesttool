/* Field configuration: list, add, edit and delete dynamic fields, plus
 * toggle required / active. Every field is fully editable and deletable —
 * there is no protected "system field" concept. field_key stays immutable
 * (it is the storage identity); all other attributes, including data_type,
 * can be changed. */
(function () {
  "use strict";
  const page = document.querySelector(".lm-fields") || document.querySelector("[data-project-id]");
  const pid = Number(page.dataset.projectId);
  const rows = document.getElementById("lm-fields-rows");
  const dialog = document.getElementById("lm-field-dialog");
  let editingId = null; // null => create mode
  let dragEl = null;     // row currently being dragged (reorder)
  let currentFields = []; // last-loaded field list, for order diffing

  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null) continue;
        if (k === "className") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "style") node.setAttribute("style", v);
        else if (k === "ariaLabel") node.setAttribute("aria-label", v);
        else if (k === "dataset") {
          for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
        } else {
          node[k] = v;
        }
      }
    }
    if (children) node.append(...children);
    return node;
  }
  function emptyRow(text) {
    return el("tr", null, [el("td", { colSpan: 9, className: "muted", text: text })]);
  }
  // The reorder grip is a real button: it is reachable with Tab, and its
  // accessible name states both the action and the keys that perform it. The
  // ⠿ glyph is decoration, so it is hidden from assistive technology.
  function gripButton(field) {
    const glyph = el("span", { text: "⠿" });
    glyph.setAttribute("aria-hidden", "true");
    const name = field.display_name || field.field_key || "";
    return el("button", {
      type: "button",
      className: "grip lm-drag-handle",
      title: "拖拽排序，或用上下方向键移动",
      ariaLabel: `调整顺序：${name}，按住拖动，或用上下方向键移动`,
    }, [glyph]);
  }

  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    t.textContent = msg; t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false; setTimeout(() => { t.hidden = true; }, 3000);
  }

  const $ = (id) => document.getElementById(id);

  function resetDialog(mode, field) {
    editingId = field ? field.id : null;
    $("lm-field-dialog-title").textContent = field ? "编辑字段" : "新增字段";
    $("lm-f-ok").textContent = field ? "保存" : "创建";
    $("lm-f-key").value = field ? field.field_key : "";
    $("lm-f-key").disabled = !!field; // field_key is immutable
    $("lm-f-name").value = field ? (field.display_name || "") : "";
    $("lm-f-sheet").value = field ? (field.sheet || "test") : "test";
    $("lm-f-type").value = field ? field.data_type : "text";
    $("lm-f-type").disabled = false; // data type can be changed at any time
    $("lm-f-options").value = field ? (field.options || []).join(", ") : "";
    $("lm-f-help").value = field ? (field.help_text || "") : "";
    $("lm-f-required").checked = field ? !!field.is_required : false;
    $("lm-f-error").hidden = true;
  }

  async function load() {
    try {
      const data = await LMApi.listFields(pid);
      currentFields = data.fields;
      const trs = data.fields.map((f) => {
        const reqNode = f.is_required
          ? el("span", { className: "pill st-on" }, [
            el("span", { className: "dot" }),
            document.createTextNode("必填"),
          ])
          : el("span", { className: "muted", text: "—" });
        const roNode = f.is_readonly
          ? el("span", { className: "tag", text: "只读" })
          : el("span", { className: "muted", text: "—" });
        return el("tr", {
          className: f.is_active ? null : "row-off",
          dataset: { id: String(f.id) },
        }, [
          el("td", null, [gripButton(f)]),
          el("td", null, [el("span", { className: "tag", text: f.sheet || "test" })]),
          el("td", null, [el("code", { text: f.field_key == null ? "" : String(f.field_key) })]),
          el("td", { text: f.display_name == null ? "" : String(f.display_name) }),
          el("td", null, [el("span", { className: "tag", text: f.data_type == null ? "" : String(f.data_type) })]),
          el("td", null, [reqNode]),
          el("td", null, [roNode]),
          el("td", {
            className: "muted",
            style: "max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap",
            text: (f.options || []).join(", ") || f.help_text || "",
          }),
          el("td", null, [el("div", { className: "row-acts" }, [
            el("button", { type: "button", className: "btn small lm-edit", text: "编辑" }),
            el("button", { type: "button", className: "btn small lm-toggle", dataset: { active: String(f.is_active) }, text: f.is_active ? "停用" : "启用" }),
            el("button", { type: "button", className: "btn small danger lm-del", text: "删除" }),
          ])]),
        ]);
      });
      rows.replaceChildren(...(trs.length ? trs : [emptyRow("暂无字段，点击右上角新增")]));
      wire(data.fields);
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      rows.replaceChildren(emptyRow(ex.message == null ? "" : String(ex.message)));
    }
  }

  // Find the row that the cursor is currently hovering above, so the dragged
  // row can be inserted before it (classic HTML5 drag-sort). Returns null when
  // the cursor is below every remaining row (append to the end).
  function rowAfter(y) {
    const els = Array.from(rows.querySelectorAll("tr:not(.lm-dragging)"));
    let closest = null;
    let closestOffset = -Infinity;
    els.forEach((el) => {
      const box = el.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closestOffset) { closestOffset = offset; closest = el; }
    });
    return closest;
  }

  // Persist the current DOM row order into display_order. Only fields whose
  // position actually changed are PATCHed. The editor page re-reads fields
  // (ordered by display_order) on focus, so the Univer table's column order
  // follows automatically.
  async function commitOrder() {
    const byId = {};
    currentFields.forEach((f) => { byId[f.id] = f; });
    const ids = Array.from(rows.querySelectorAll("tr"))
      .map((tr) => Number(tr.dataset.id));
    const patches = [];
    ids.forEach((id, i) => {
      const f = byId[id];
      if (f && f.display_order !== i) patches.push({ id: id, order: i });
    });
    if (!patches.length) return;
    try {
      for (const p of patches) {
        await LMApi.patchField(pid, p.id, { display_order: p.order });
      }
      await load();
    } catch (ex) { toast(ex.message, false); await load(); }
  }

  // Keyboard reorder: the grip exposes ArrowUp/ArrowDown (one step) and
  // Home/End (first/last). Order is table-global, so an inactive row moves
  // exactly like an active one. The row node itself is moved, never the row
  // re-rendered, so the grip keeps focus; the live region below is persistent
  // and only its text changes.
  function orderedRows() {
    return Array.from(rows.querySelectorAll("tr[data-id]"));
  }

  function gripFor(id) {
    for (const tr of orderedRows()) {
      if (Number(tr.dataset.id) === Number(id)) {
        return tr.querySelector(".lm-drag-handle");
      }
    }
    return null;
  }

  function focusGrip(id) {
    const grip = gripFor(id);
    if (!grip) return;
    const tr = grip.closest("tr");
    if (tr && typeof tr.scrollIntoView === "function") {
      tr.scrollIntoView({ block: "nearest" });
    }
    grip.focus({ preventScroll: true });
  }

  let announceTimer = 0;

  // A live region only speaks when its text changes, so the previous message
  // is cleared first: moving the same field twice must be announced twice.
  function announce(message) {
    const live = document.getElementById("lm-fields-status");
    if (!live) return;
    clearTimeout(announceTimer);
    live.textContent = "";
    announceTimer = setTimeout(() => { live.textContent = message; }, 80);
  }

  function moveRowTo(tr, targetIndex) {
    const list = orderedRows();
    const from = list.indexOf(tr);
    if (from < 0) return -1;
    const to = Math.max(0, Math.min(list.length - 1, targetIndex));
    if (to === from) return -1;
    const ref = list[to];
    const before = to > from ? ref.nextElementSibling : ref;
    // moveBefore() carries focus and element state across the move; the
    // insertBefore() fallback covers browsers without it.
    if (typeof rows.moveBefore === "function") rows.moveBefore(tr, before);
    else rows.insertBefore(tr, before);
    return to;
  }

  async function reorderField(tr, targetIndex) {
    const id = Number(tr.dataset.id);
    const field = currentFields.find((f) => Number(f.id) === id);
    const to = moveRowTo(tr, targetIndex);
    if (to < 0) return false;
    const name = field ? (field.display_name || field.field_key) : "";
    announce(`「${name}」已移动到第 ${to + 1} 位`);
    focusGrip(id);
    await commitOrder(); // reloads the list, so re-focus the rebuilt grip
    focusGrip(id);
    return true;
  }

  function onGripKey(ev, grip) {
    const key = ev.key;
    if (key !== "ArrowUp" && key !== "ArrowDown" && key !== "Home" && key !== "End") return;
    if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
    ev.preventDefault(); // a handled reorder key must not scroll the page
    const tr = grip.closest("tr");
    if (!tr) return;
    const list = orderedRows();
    const from = list.indexOf(tr);
    if (from < 0) return;
    let to = from;
    if (key === "ArrowUp") to = from - 1;
    else if (key === "ArrowDown") to = from + 1;
    else if (key === "Home") to = 0;
    else to = list.length - 1;
    if (to === from || to < 0 || to >= list.length) return; // ends are no-ops
    reorderField(tr, to);
  }

  // Drag-to-reorder, registered ONCE on the persistent <tbody>. Rows are
  // rebuilt on every load(), but this delegated wiring survives, so it must
  // not be re-added inside wire() (that would stack duplicate handlers and
  // fire commitOrder repeatedly). A row is only made draggable while its
  // handle is held, so the action buttons inside the row keep working.
  function initDragSort() {
    rows.addEventListener("mousedown", (e) => {
      const h = e.target.closest ? e.target.closest(".lm-drag-handle") : null;
      if (!h) return;
      const tr = h.closest("tr");
      if (tr) tr.draggable = true;
    });
    document.addEventListener("mouseup", () => {
      if (dragEl) return; // an active drag clears itself on dragend
      const stray = rows.querySelector('tr[draggable="true"]');
      if (stray) stray.draggable = false;
    });
    rows.addEventListener("dragstart", (e) => {
      const tr = e.target.closest ? e.target.closest("tr") : null;
      if (!tr || !tr.draggable) return;
      dragEl = tr;
      tr.classList.add("lm-dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", tr.dataset.id); } catch (_e) { /* noop */ }
      }
    });
    rows.addEventListener("dragover", (e) => {
      if (!dragEl) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      const after = rowAfter(e.clientY);
      if (after == null) rows.appendChild(dragEl);
      else if (after !== dragEl) rows.insertBefore(dragEl, after);
    });
    rows.addEventListener("drop", (e) => { if (dragEl) e.preventDefault(); });
    rows.addEventListener("dragend", async () => {
      if (!dragEl) return;
      dragEl.classList.remove("lm-dragging");
      dragEl.draggable = false;
      dragEl = null;
      await commitOrder();
    });
  }

  function wire(fieldList) {
    const byId = {};
    fieldList.forEach((f) => { byId[f.id] = f; });

    rows.querySelectorAll(".lm-drag-handle").forEach((b) => {
      b.addEventListener("keydown", (ev) => onGripKey(ev, b));
    });

    rows.querySelectorAll(".lm-toggle").forEach((b) => {
      b.addEventListener("click", async () => {
        const id = Number(b.closest("tr").dataset.id);
        try {
          await LMApi.patchField(pid, id, { is_active: b.dataset.active !== "true" });
          await load();
        } catch (ex) { toast(ex.message, false); }
      });
    });

    rows.querySelectorAll(".lm-edit").forEach((b) => {
      b.addEventListener("click", () => {
        const id = Number(b.closest("tr").dataset.id);
        resetDialog("edit", byId[id]);
        dialog.showModal();
      });
    });

    rows.querySelectorAll(".lm-del").forEach((b) => {
      b.addEventListener("click", async () => {
        const id = Number(b.closest("tr").dataset.id);
        const f = byId[id];
        // Schema-level change: wipes a column across every test item.
        if (!(await LMUI.confirm({
          level: "critical",
          title: `删除字段「${f.display_name || f.field_key}」`,
          body: "该字段在所有测试项中的数据将被永久清除，且无法恢复。",
          requireText: f.field_key,
          confirmText: "永久删除字段",
        }))) return;
        try {
          await LMApi.deleteField(pid, id);
          await load();
          toast("字段已删除", true);
        } catch (ex) { toast(ex.message, false); }
      });
    });
  }

  $("lm-add-field").addEventListener("click", () => {
    resetDialog("create", null);
    dialog.showModal();
  });

  $("lm-f-ok").addEventListener("click", async (e) => {
    e.preventDefault();
    const err = $("lm-f-error");
    const opts = $("lm-f-options").value.split(",").map((s) => s.trim()).filter(Boolean);
    try {
      if (editingId == null) {
        await LMApi.addField(pid, {
          field_key: $("lm-f-key").value.trim(),
          display_name: $("lm-f-name").value.trim(),
          sheet: $("lm-f-sheet").value,
          data_type: $("lm-f-type").value,
          options: opts,
          help_text: $("lm-f-help").value.trim(),
          is_required: $("lm-f-required").checked,
        });
        toast("字段已创建", true);
      } else {
        await LMApi.patchField(pid, editingId, {
          display_name: $("lm-f-name").value.trim(),
          sheet: $("lm-f-sheet").value,
          data_type: $("lm-f-type").value,
          options: opts,
          help_text: $("lm-f-help").value.trim(),
          is_required: $("lm-f-required").checked,
        });
        toast("字段已更新", true);
      }
      dialog.close();
      await load();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });

  initDragSort();
  load();
})();
