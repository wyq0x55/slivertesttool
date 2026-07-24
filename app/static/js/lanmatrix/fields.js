/* Field configuration: list, add, edit and delete dynamic fields, plus
 * toggle required / active. Every field is fully editable and deletable —
 * there is no protected "system field" concept. field_key stays immutable
 * (it is the storage identity); all other attributes, including data_type,
 * can be changed. */
(function () {
  "use strict";
  const page = document.querySelector(".lm-page");
  const pid = Number(page.dataset.projectId);
  const rows = document.getElementById("lm-fields-rows");
  const dialog = document.getElementById("lm-field-dialog");
  let editingId = null; // null => create mode
  let dragEl = null;     // row currently being dragged (reorder)
  let currentFields = []; // last-loaded field list, for order diffing

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
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
      rows.innerHTML = data.fields.map((f) => `
        <tr data-id="${f.id}">
          <td class="lm-order">
            <span class="lm-drag-handle" title="拖拽排序" aria-label="拖拽排序">⠿</span>
          </td>
          <td><span class="lm-badge">${esc(f.sheet || "test")}</span></td>
          <td><code>${esc(f.field_key)}</code></td>
          <td>${esc(f.display_name)}</td>
          <td>${esc(f.data_type)}</td>
          <td>${f.is_required ? "✓" : ""}</td>
          <td>${f.is_readonly ? "✓" : ""}</td>
          <td class="lm-muted">${esc((f.options || []).join(", ") || f.help_text || "")}</td>
          <td class="lm-actions">
            <button class="lm-btn lm-btn-sm lm-edit">编辑</button>
            <button class="lm-btn lm-btn-sm lm-toggle" data-active="${f.is_active}">${f.is_active ? "停用" : "启用"}</button>
            <button class="lm-btn lm-btn-sm lm-del">删除</button>
          </td>
        </tr>`).join("");
      wire(data.fields);
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      rows.innerHTML = `<tr><td colspan="9" class="lm-err">${esc(ex.message)}</td></tr>`;
    }
  }

  // Find the row that the cursor is currently hovering above, so the dragged
  // row can be inserted before it (classic HTML5 drag-sort). Returns null when
  // the cursor is below every remaining row (append to the end).
  function rowAfter(y) {
    const els = Array.prototype.slice.call(
      rows.querySelectorAll("tr:not(.lm-dragging)"));
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
    const ids = Array.prototype.slice.call(rows.querySelectorAll("tr"))
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
        if (!confirm(`确定删除字段「${f.display_name || f.field_key}」？该字段在所有测试项中的数据将被清除。`)) return;
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
