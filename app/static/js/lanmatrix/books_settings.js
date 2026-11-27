/* Specification-book registry for Silver multi-book projects. */
(function () {
  "use strict";
  const page = document.querySelector(".lm-books-settings");
  if (!page) return;
  const pid = Number(page.dataset.projectId);
  const rowsEl = document.getElementById("lm-book-rows");
  const createPanel = document.getElementById("lm-book-create-panel");
  const createName = document.getElementById("lm-book-create-name");
  const createModel = document.getElementById("lm-book-create-model");
  const createBtn = document.getElementById("lm-book-create");
  let books = [];
  let models = [];
  let canEdit = false;

  const esc = (value) => String(value == null ? "" : value).replace(
    /[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

  function toast(message, ok) {
    const el = document.getElementById("lm-toast");
    if (!el) return;
    el.textContent = message;
    el.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 3000);
  }

  function showError(message) {
    const el = document.getElementById("lm-book-error");
    if (!el) return;
    el.textContent = message || "";
    el.hidden = !message;
  }

  function modelLabel(model) {
    return model.version ? `${model.name}@${model.version}` : model.name;
  }

  function render() {
    if (!books.length) {
      rowsEl.innerHTML = '<tr><td colspan="6"><div class="state">' +
        '<h3>尚未建立式样书</h3><p>在测试矩阵中导入日文式样书，或在此先创建空白式样书再导入。</p></div></td></tr>';
      return;
    }
    rowsEl.innerHTML = books.map((book) => {
      const label = book.model ? modelLabel(book.model) : "未绑定";
      return `<tr data-book-id="${book.id}">
        <td>
          <input class="ipt lm-book-name" data-book-id="${book.id}"
                 value="${esc(book.name)}" maxlength="120" aria-label="式样书名称"
                 ${canEdit ? "" : "disabled"}>
        </td>
        <td><code>${esc(book.source_filename || "—")}</code></td>
        <td>${esc(label)}</td>
        <td>${Number(book.test_count) || 0}</td>
        <td>${Number(book.io_count) || 0}</td>
        <td><span class="row-acts">
          ${canEdit ? `<button type="button" class="btn small lm-book-save" data-book-id="${book.id}">保存名称</button>
          <button type="button" class="btn small danger lm-book-delete" data-book-id="${book.id}" data-name="${esc(book.name)}">删除</button>` : "—"}
        </span></td>
      </tr>`;
    }).join("");
    rowsEl.querySelectorAll(".lm-book-save").forEach((button) => {
      button.addEventListener("click", () => saveName(Number(button.dataset.bookId)));
    });
    rowsEl.querySelectorAll(".lm-book-delete").forEach((button) => {
      button.addEventListener("click", () => removeBook(Number(button.dataset.bookId), button.dataset.name));
    });
  }

  function fillModelSelect() {
    createModel.innerHTML = '<option value="">请选择模型</option>' + models.map((model) =>
      `<option value="${model.id}">${esc(modelLabel(model))}</option>`
    ).join("");
    createBtn.disabled = !canEdit || !models.length;
    if (!models.length) {
      createModel.innerHTML = '<option value="">请先在“模型管理”中添加模型</option>';
    }
  }

  async function load() {
    const status = document.getElementById("lm-book-status");
    if (status) status.textContent = "加载中…";
    try {
      const [bookData, modelData] = await Promise.all([
        LMApi.listBooks(pid),
        LMApi.listProjectModels(pid),
      ]);
      books = (bookData && bookData.books) || [];
      canEdit = !!(bookData && bookData.can_edit);
      models = (modelData && modelData.models) || [];
      if (createPanel) createPanel.hidden = !canEdit;
      fillModelSelect();
      render();
      if (status) status.textContent = `共 ${books.length} 本`;
    } catch (ex) {
      rowsEl.innerHTML = `<tr><td colspan="6"><div class="state err"><h3>加载失败</h3><p>${esc(ex.message)}</p></div></td></tr>`;
      if (status) status.textContent = "";
    }
  }

  async function saveName(bookId) {
    const input = rowsEl.querySelector(`.lm-book-name[data-book-id="${bookId}"]`);
    if (!input) return;
    const name = input.value.trim();
    if (!name) {
      showError("式样书名称不能为空");
      input.focus();
      return;
    }
    try {
      await LMApi.updateBook(pid, bookId, { name });
      showError("");
      toast("式样书名称已更新", true);
      await load();
    } catch (ex) {
      showError(ex.message);
    }
  }

  async function removeBook(bookId, name) {
    if (window.LMUI && LMUI.confirm) {
      const ok = await LMUI.confirm({
        level: "danger",
        title: `删除式样书「${name}」`,
        body: "只允许删除没有任何正文、回收站数据或测试任务的式样书，原书文件也会被删除。",
        confirmText: "删除",
      });
      if (!ok) return;
    } else if (!window.confirm(`删除式样书「${name}」？`)) {
      return;
    }
    try {
      await LMApi.deleteBook(pid, bookId);
      toast("式样书已删除", true);
      await load();
    } catch (ex) {
      toast(ex.message, false);
    }
  }

  createBtn.addEventListener("click", async () => {
    showError("");
    const name = createName.value.trim();
    const modelId = createModel.value ? Number(createModel.value) : null;
    if (!name) {
      showError("请填写式样书名称");
      createName.focus();
      return;
    }
    if (!modelId) {
      showError("请选择绑定的模型版本");
      createModel.focus();
      return;
    }
    createBtn.disabled = true;
    try {
      await LMApi.createBook(pid, { name, model_id: modelId });
      createName.value = "";
      toast("式样书已创建", true);
      await load();
    } catch (ex) {
      showError(ex.message);
    } finally {
      createBtn.disabled = false;
    }
  });

  load();
})();
