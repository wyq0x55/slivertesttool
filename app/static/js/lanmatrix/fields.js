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
      const n = data.fields.length;
      rows.innerHTML = data.fields.map((f, i) => `
        <tr data-id="${f.id}">
          <td class="lm-order">
            <button class="lm-btn lm-btn-sm lm-move-up" title="上移" ${i === 0 ? "disabled" : ""}>↑</button>
            <button class="lm-btn lm-btn-sm lm-move-down" title="下移" ${i === n - 1 ? "disabled" : ""}>↓</button>
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

  // Reorder by swapping display_order with the adjacent field. The editor page
  // re-reads fields (ordered by display_order) on focus, so the Univer table's
  // column order follows automatically.
  async function moveField(fieldList, id, dir) {
    const idx = fieldList.findIndex((f) => f.id === id);
    if (idx < 0) return;
    const j = dir === "up" ? idx - 1 : idx + 1;
    if (j < 0 || j >= fieldList.length) return;
    const a = fieldList[idx];
    const b = fieldList[j];
    const ao = a.display_order == null ? idx : a.display_order;
    const bo = b.display_order == null ? j : b.display_order;
    let na = bo;
    let nb = ao;
    if (na === nb) { na = j; nb = idx; } // break ties from legacy null orders
    try {
      await LMApi.patchField(pid, a.id, { display_order: na });
      await LMApi.patchField(pid, b.id, { display_order: nb });
      await load();
    } catch (ex) { toast(ex.message, false); }
  }

  function wire(fieldList) {
    const byId = {};
    fieldList.forEach((f) => { byId[f.id] = f; });

    rows.querySelectorAll(".lm-move-up").forEach((b) => {
      b.addEventListener("click", () => {
        moveField(fieldList, Number(b.closest("tr").dataset.id), "up");
      });
    });
    rows.querySelectorAll(".lm-move-down").forEach((b) => {
      b.addEventListener("click", () => {
        moveField(fieldList, Number(b.closest("tr").dataset.id), "down");
      });
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

  load();
})();
