/* System-admin PostgreSQL management console.
 *
 * Renders a connection overview + per-table stats and a guarded SQL console.
 * All data flows through /api/v1/admin/db/* which is server-side gated to
 * ``is_system_admin`` users (the UI here is a convenience, not the authority).
 */
(function () {
  "use strict";
  const root = document.querySelector(".lm-dbadmin");
  if (!root) return;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3200);
  }

  function fmtInt(n) {
    return (n == null ? 0 : n).toLocaleString("en-US");
  }

  async function loadOverview() {
    const info = document.getElementById("lm-db-info");
    info.textContent = "加载中…";
    try {
      const d = await LMApi.dbOverview();
      if (d.error) { info.innerHTML = `<span class="lm-err">${esc(d.error)}</span>`; }
      else {
        info.innerHTML = `
          <div class="lm-db-kv"><span>后端</span><b>${esc(d.backend)}</b></div>
          <div class="lm-db-kv"><span>数据库</span><b>${esc(d.database)}</b></div>
          <div class="lm-db-kv"><span>连接用户</span><b>${esc(d.db_user)}</b></div>
          <div class="lm-db-kv"><span>服务器</span><b>${esc(d.server_addr || "本地/socket")}${d.server_port ? ":" + esc(d.server_port) : ""}</b></div>
          <div class="lm-db-kv"><span>数据库大小</span><b>${esc(d.size_pretty)}</b></div>
          <div class="lm-db-kv"><span>服务器时间</span><b>${esc(d.now)}</b></div>
          <div class="lm-db-kv lm-db-kv-wide"><span>版本</span><b>${esc(d.version)}</b></div>`;
      }
      const tbody = document.getElementById("lm-db-tables");
      const tables = d.tables || [];
      document.getElementById("lm-db-tblcount").textContent =
        tables.length ? `共 ${tables.length} 张` : "";
      tbody.innerHTML = tables.map((t) => `
        <tr>
          <td>${esc(t.schema)}</td>
          <td class="lm-db-tname">${esc(t.name)}</td>
          <td class="num">${fmtInt(t.est_rows)}</td>
          <td class="num">${esc(t.size_pretty)}</td>
          <td><button class="lm-btn lm-btn-sm lm-db-peek" data-t="${esc(t.schema)}.${esc(t.name)}">预览</button></td>
        </tr>`).join("") ||
        `<tr><td colspan="5" class="lm-muted">无用户表</td></tr>`;
      tbody.querySelectorAll(".lm-db-peek").forEach((b) => {
        b.addEventListener("click", () => {
          const name = b.dataset.t;
          document.getElementById("lm-db-sql").value = `SELECT * FROM ${name} LIMIT 100;`;
          setReadOnly(true);
          runSql();
        });
      });
    } catch (ex) {
      info.innerHTML = `<span class="lm-err">${esc(ex.message || "加载失败")}</span>`;
      if (ex.status === 403) toast("仅系统管理员可访问", false);
    }
  }

  function setReadOnly(on) {
    document.getElementById("lm-db-readonly").checked = on;
    document.getElementById("lm-db-write-warn").hidden = on;
  }

  function renderResult(d) {
    const head = document.getElementById("lm-db-result-head");
    const body = document.getElementById("lm-db-result-body");
    const meta = document.getElementById("lm-db-meta");
    if (d.returns_rows) {
      head.innerHTML = `<tr>${(d.columns || []).map((c) => `<th>${esc(c)}</th>`).join("")}</tr>`;
      body.innerHTML = (d.rows || []).map((r) =>
        `<tr>${r.map((v) => `<td>${v === null ? '<span class="lm-null">NULL</span>' : esc(v)}</td>`).join("")}</tr>`
      ).join("") || `<tr><td class="lm-muted">（无数据）</td></tr>`;
      meta.textContent = `返回 ${d.rowcount} 行` +
        (d.truncated ? `（已截断，最多显示前 ${d.rows.length} 行）` : "") +
        ` · ${d.command} · ${d.elapsed_ms} ms` + (d.read_only ? " · 只读" : " · 已提交");
    } else {
      head.innerHTML = "";
      body.innerHTML = "";
      meta.textContent = `影响 ${d.rowcount} 行 · ${d.command} · ${d.elapsed_ms} ms` +
        (d.read_only ? " · 只读(已回滚)" : " · 已提交");
    }
  }

  let running = false;
  async function runSql() {
    if (running) return;
    const sql = document.getElementById("lm-db-sql").value.trim();
    const err = document.getElementById("lm-db-error");
    err.hidden = true;
    if (!sql) { err.textContent = "请输入 SQL 语句"; err.hidden = false; return; }
    const readOnly = document.getElementById("lm-db-readonly").checked;
    if (!readOnly && !confirm("写模式将直接提交到数据库，确定执行？")) return;
    running = true;
    const btn = document.getElementById("lm-db-run");
    btn.disabled = true;
    try {
      const d = await LMApi.dbQuery(sql, readOnly);
      renderResult(d);
      if (!readOnly) { toast("已执行并提交", true); loadOverview(); }
    } catch (ex) {
      err.textContent = ex.message || "执行失败";
      err.hidden = false;
      document.getElementById("lm-db-meta").textContent = "";
    } finally {
      running = false;
      btn.disabled = false;
    }
  }

  document.getElementById("lm-db-refresh").addEventListener("click", () => {
    loadOverview();
    if (currentTable) loadTableRows();
  });
  document.getElementById("lm-db-run").addEventListener("click", runSql);
  document.getElementById("lm-db-readonly").addEventListener("change", (e) => {
    document.getElementById("lm-db-write-warn").hidden = e.target.checked;
  });
  document.getElementById("lm-db-sql").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runSql(); }
  });

  // --- Tabs ------------------------------------------------------------- #
  document.querySelectorAll(".lm-db-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".lm-db-tab").forEach((t) => t.classList.remove("is-active"));
      tab.classList.add("is-active");
      const which = tab.dataset.tab;
      document.querySelectorAll(".lm-db-pane").forEach((p) => {
        p.hidden = p.dataset.pane !== which;
      });
    });
  });

  // --- Table browser + no-SQL CRUD -------------------------------------- #
  let currentTable = null;
  let currentSchema = null;
  let tvPage = 1;
  const tvPageSize = 50;
  let tvOrderBy = null;
  let tvDesc = false;

  async function loadTableList() {
    const nav = document.getElementById("lm-db-tablenav");
    try {
      const d = await LMApi.dbTables();
      nav.innerHTML = (d.tables || []).map((t) =>
        `<li><button class="lm-db-tnav-btn" data-t="${esc(t.name)}">
           <span class="lm-db-tnav-name">${esc(t.name)}</span>
           <span class="lm-db-tnav-rows">${fmtInt(t.est_rows)}</span>
         </button></li>`).join("") || `<li class="lm-muted">无表</li>`;
      nav.querySelectorAll(".lm-db-tnav-btn").forEach((b) => {
        b.addEventListener("click", () => selectTable(b.dataset.t));
      });
    } catch (ex) {
      nav.innerHTML = `<li class="lm-err">${esc(ex.message || "加载失败")}</li>`;
    }
  }

  async function selectTable(name) {
    currentTable = name;
    tvPage = 1;
    tvOrderBy = null;
    tvDesc = false;
    document.querySelectorAll(".lm-db-tnav-btn").forEach((b) =>
      b.classList.toggle("is-active", b.dataset.t === name));
    try {
      currentSchema = await LMApi.dbTableSchema(name);
    } catch (ex) {
      showTvError(ex.message);
      return;
    }
    renderTableHead();
    await loadTableRows();
  }

  function renderTableHead() {
    const head = document.getElementById("lm-db-tv-head");
    const pk = (currentSchema.primary_key || []);
    const canInsert = true;
    head.innerHTML = `
      <strong class="lm-db-tv-name">${esc(currentTable)}</strong>
      <span class="lm-muted">${(currentSchema.columns || []).length} 列 · 主键：${pk.length ? esc(pk.join(", ")) : "无"}</span>
      <span class="lm-db-tv-actions">
        <button class="lm-btn lm-btn-sm lm-btn-primary" id="lm-db-add-row"${canInsert ? "" : " disabled"}>＋ 新增行</button>
      </span>`;
    document.getElementById("lm-db-add-row").addEventListener("click", () => openRowDialog(null));
  }

  function showTvError(msg) {
    const e = document.getElementById("lm-db-tv-error");
    e.textContent = msg || "";
    e.hidden = !msg;
  }

  async function loadTableRows() {
    showTvError("");
    try {
      const d = await LMApi.dbTableRows(currentTable, {
        page: tvPage, page_size: tvPageSize,
        order_by: tvOrderBy || "", desc: tvDesc ? "1" : "",
      });
      renderRows(d);
    } catch (ex) {
      showTvError(ex.message);
    }
  }

  function renderRows(d) {
    const pk = new Set(d.primary_key || []);
    const cols = d.columns || [];
    const head = document.getElementById("lm-db-grid-head");
    const body = document.getElementById("lm-db-grid-body");
    const hasPk = (d.primary_key || []).length > 0;
    head.innerHTML = `<tr>${cols.map((c) => {
      const isPk = pk.has(c);
      const arrow = tvOrderBy === c ? (tvDesc ? " ▼" : " ▲") : "";
      return `<th class="lm-db-sortable${isPk ? " lm-db-pk" : ""}" data-c="${esc(c)}">${esc(c)}${isPk ? " 🔑" : ""}${arrow}</th>`;
    }).join("")}<th class="lm-db-opcol">操作</th></tr>`;
    head.querySelectorAll(".lm-db-sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const c = th.dataset.c;
        if (tvOrderBy === c) tvDesc = !tvDesc; else { tvOrderBy = c; tvDesc = false; }
        tvPage = 1;
        loadTableRows();
      });
    });
    body.innerHTML = (d.rows || []).map((row) => {
      const cells = row.map((v) =>
        `<td>${v === null ? '<span class="lm-null">NULL</span>' : esc(truncate(v))}</td>`).join("");
      const pkObj = {};
      (d.primary_key || []).forEach((c) => { pkObj[c] = row[cols.indexOf(c)]; });
      const pkAttr = esc(JSON.stringify(pkObj));
      const rowAttr = esc(JSON.stringify(row));
      const ops = hasPk
        ? `<button class="lm-btn lm-btn-sm lm-db-edit" data-pk='${pkAttr}' data-row='${rowAttr}'>编辑</button>
           <button class="lm-btn lm-btn-sm lm-db-del" data-pk='${pkAttr}'>删除</button>`
        : `<span class="lm-muted" title="无主键，无法定位单行">—</span>`;
      return `<tr>${cells}<td class="lm-db-opcol">${ops}</td></tr>`;
    }).join("") || `<tr><td colspan="${cols.length + 1}" class="lm-muted">（无数据）</td></tr>`;

    body.querySelectorAll(".lm-db-edit").forEach((b) => {
      b.addEventListener("click", () => {
        const pkObj = JSON.parse(b.dataset.pk);
        const rowArr = JSON.parse(b.dataset.row);
        const rowObj = {};
        cols.forEach((c, i) => { rowObj[c] = rowArr[i]; });
        openRowDialog({ pk: pkObj, row: rowObj });
      });
    });
    body.querySelectorAll(".lm-db-del").forEach((b) => {
      b.addEventListener("click", () => deleteRow(JSON.parse(b.dataset.pk)));
    });

    document.getElementById("lm-db-tv-count").textContent = `共 ${fmtInt(d.total)} 行`;
    renderTvPager(d.pages, d.page);
  }

  function truncate(v) {
    const s = String(v);
    return s.length > 200 ? s.slice(0, 200) + "…" : s;
  }

  function renderTvPager(pages, page) {
    const el = document.getElementById("lm-db-tv-pager");
    el.innerHTML = "";
    if (pages <= 1) return;
    const mk = (label, target, disabled) => {
      const b = document.createElement("button");
      b.className = "lm-btn lm-btn-sm";
      b.textContent = label;
      b.disabled = disabled;
      b.addEventListener("click", () => { tvPage = target; loadTableRows(); });
      return b;
    };
    el.appendChild(mk("上一页", page - 1, page <= 1));
    const span = document.createElement("span");
    span.className = "lm-muted";
    span.textContent = ` ${page} / ${pages} `;
    el.appendChild(span);
    el.appendChild(mk("下一页", page + 1, page >= pages));
  }

  // --- Row insert/edit dialog ------------------------------------------- #
  const rowDialog = document.getElementById("lm-db-row-dialog");
  let editingPk = null;   // null → insert mode

  function openRowDialog(ctx) {
    editingPk = ctx ? ctx.pk : null;
    const isEdit = !!ctx;
    document.getElementById("lm-db-row-title").textContent =
      (isEdit ? "编辑行 · " : "新增行 · ") + currentTable;
    document.getElementById("lm-db-row-error").hidden = true;
    const form = document.getElementById("lm-db-row-form");
    const rowObj = ctx ? ctx.row : {};
    form.innerHTML = (currentSchema.columns || []).map((c) => {
      const val = ctx ? rowObj[c.name] : (c.default !== null && !isEdit ? "" : "");
      const isPk = c.is_pk;
      const disabled = isEdit && isPk;  // don't allow editing the PK in place
      const meta = [];
      meta.push(esc(c.udt_name || c.data_type));
      if (isPk) meta.push("主键");
      if (!c.nullable) meta.push("NOT NULL");
      if (c.auto) meta.push("可自动");
      const isBool = (c.udt_name || "").toLowerCase() === "bool";
      const multiline = (c.udt_name || "").toLowerCase() === "text";
      let control;
      const curVal = val === null || val === undefined ? "" : val;
      if (isBool) {
        control = `<select class="lm-input lm-db-field" data-c="${esc(c.name)}"${disabled ? " disabled" : ""}>
            <option value=""${curVal === "" ? " selected" : ""}>（空/默认）</option>
            <option value="true"${curVal === true || curVal === "true" ? " selected" : ""}>true</option>
            <option value="false"${curVal === false || curVal === "false" ? " selected" : ""}>false</option>
          </select>`;
      } else if (multiline) {
        control = `<textarea class="lm-input lm-db-field lm-db-field-ta" data-c="${esc(c.name)}"${disabled ? " disabled" : ""}>${esc(curVal)}</textarea>`;
      } else {
        control = `<input class="lm-input lm-db-field" data-c="${esc(c.name)}" value="${esc(curVal)}"${disabled ? " disabled" : ""}>`;
      }
      const placeholder = c.auto && !isEdit ? " (留空用默认值)" : "";
      return `<label class="lm-db-field-row">
          <span class="lm-db-field-label">${esc(c.name)}<em>${meta.join(" · ")}${placeholder}</em></span>
          ${control}
        </label>`;
    }).join("");
    rowDialog.showModal();
  }

  document.getElementById("lm-db-row-save").addEventListener("click", async (e) => {
    e.preventDefault();
    const errEl = document.getElementById("lm-db-row-error");
    errEl.hidden = true;
    const values = {};
    document.querySelectorAll("#lm-db-row-form .lm-db-field").forEach((f) => {
      if (f.disabled) return;
      values[f.dataset.c] = f.value;
    });
    try {
      if (editingPk) {
        await LMApi.dbUpdateRow(currentTable, editingPk, values);
        toast("已更新", true);
      } else {
        await LMApi.dbInsertRow(currentTable, values);
        toast("已新增", true);
      }
      rowDialog.close();
      await loadTableRows();
    } catch (ex) {
      errEl.textContent = ex.message || "保存失败";
      errEl.hidden = false;
    }
  });

  async function deleteRow(pk) {
    if (!confirm("确定删除该行？此操作不可撤销。")) return;
    try {
      await LMApi.dbDeleteRow(currentTable, pk);
      toast("已删除", true);
      await loadTableRows();
    } catch (ex) {
      showTvError(ex.message);
    }
  }

  window.LMReady.then(() => { loadOverview(); loadTableList(); });
})();
