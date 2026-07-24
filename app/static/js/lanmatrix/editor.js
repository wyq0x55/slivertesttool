/* Editor orchestration: load project/fields/items, wire toolbar,
 * import wizard, export dialog and pagination. */
(function () {
  "use strict";
  const root = document.querySelector(".lm-editor");
  const pid = Number(root.dataset.projectId);
  let fields = [];
  let grid = null;
  let collab = null;            // LMCollabController instance when real-time collab is active
  const collabAvailable = root.dataset.collab === "1";  // server shipped the collab bundle + flag
  const collabActive = () => !!(collab && collab.isActive());
  // Editor sheet catalogue + protocol constants. The SINGLE source of truth is
  // the server (GET /api/v1/config → app/services/lanmatrix/fields.py); init()
  // fetches it and fills these in. The literals below are only a last-resort
  // fallback used when that request fails, so the editor still renders — they
  // are NOT a parallel schema definition.
  let currentSheet = "test";    // active editor sheet (overwritten from config.default_sheet)
  let SHEET_SPECS = [
    { key: "test", name: "测试用例" },
    { key: "const", name: "常量" },
    { key: "lib", name: "函数库" },
  ];
  let SHEET_KEYS = SHEET_SPECS.map((s) => s.key);
  // field_key carrying the test-procedure (手順) JSON per sheet; sheets missing
  // here have no steps editor.
  let SHEET_STEPS = { test: "steps", lib: "lib_stb" };
  // CRDT Y.Array key prefix (config.row_array_prefix), handed to the collab layer.
  let ROW_ARRAY_PREFIX = "rows:";
  // Pull the canonical protocol config from the server, overriding the fallback
  // schema above. Any failure keeps the fallback so the editor stays usable.
  async function loadConfig() {
    try {
      const cfg = await LMApi.getConfig();
      if (cfg && Array.isArray(cfg.sheets) && cfg.sheets.length) {
        SHEET_SPECS = cfg.sheets.map((s) => ({ key: s.key, name: s.name }));
        SHEET_KEYS = SHEET_SPECS.map((s) => s.key);
        if (cfg.steps_fields) SHEET_STEPS = cfg.steps_fields;
        if (cfg.default_sheet) currentSheet = cfg.default_sheet;
        if (cfg.row_array_prefix) ROW_ARRAY_PREFIX = cfg.row_array_prefix;
      }
    } catch (_e) { /* keep fallback schema */ }
  }
  // Cache of each sheet's field list (used to drive per-sheet logic when running
  // the native-multi-sheet Univer engine).
  const sheetFields = {};
  // Cache of each sheet's full row list (all テスト区分), keyed by sheet id. The
  // test sheet is then paginated by category into the grid; const/lib show flat.
  const sheetItems = {};
  const isUniver = () => grid && grid.engine === "univer";
  // Native multi-sheet rendering needs the rebuilt Univer bundle (adapter.ts
  // exposes setSheetFields / setSheetData). Older bundles only have the generic
  // setFields / setData. Feature-detect so a stale bundle degrades to a single
  // active sheet instead of throwing "setSheetFields is not a function".
  const hasNativeSheets = () =>
    isUniver() && typeof grid.setSheetFields === "function"
               && typeof grid.setSheetData === "function";
  function pushFields(key, flds) {
    if (hasNativeSheets()) grid.setSheetFields(key, flds);
    else if (grid) grid.setFields(flds);
  }
  // Native Univer with the incremental patch API (rebuilt bundle, design §1.3).
  const hasIncrementalPatch = () =>
    hasNativeSheets() && typeof grid.patchSheetData === "function";
  // `incremental` is only set for real-time collab remote syncs: it writes just
  // the changed cells so scroll/selection/active-edit survive. Every other push
  // (initial load, tab switch, pagination, defensive repaint) does a full render.
  function pushData(key, items, incremental) {
    if (hasNativeSheets()) {
      if (incremental && hasIncrementalPatch()) grid.patchSheetData(key, items);
      else grid.setSheetData(key, items);
    } else if (grid) {
      grid.setData(items);
    }
  }
  let page = 1;                 // 1-based index into categoryPages (one page per テスト区分)
  const FETCH_CHUNK = 500;      // server-side page size used when fetching all rows
  let quick = "";
  let categoryPages = [];       // [{ key, label, items }] — one entry per テスト区分
  let total = 0;                // total row count across all categories

  // --- Background silent sync (multi-user collaboration) ---
  // The server stays authoritative and every edit PATCHes with an optimistic
  // version check, so conflicts are already handled. But without a live channel
  // a user won't SEE a teammate's change until they act. This lightweight poll
  // closes that gap: every POLL_MS it re-fetches the current page and, only if
  // the data actually changed AND the user is not mid-edit, silently re-renders.
  const POLL_MS = 8000;
  let pollTimer = null;
  let lastSig = "";       // signature of the last rendered dataset (id:version)
  let savingCount = 0;    // in-flight PATCHes — never refresh over an active save

  function dataSignature(items, total) {
    let s = "n=" + total + ";";
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      s += it.id + ":" + (it.version || 0) + ";";
    }
    return s;
  }

  // A refresh is unsafe while the user is typing in a cell, while a save is in
  // flight, when the tab is hidden, or when a modal (batch/import) is open —
  // those flows own their own reloads. Skipping keeps edits from being clobbered.
  function refreshUnsafe() {
    if (!grid) return true;
    if (savingCount > 0) return true;
    if (document.hidden) return true;
    if (document.querySelector("dialog[open]")) return true;
    // Prefer the grid's OWN edit-state signal (Univer adapter: SheetEditStarted/
    // Ended; fallback grid: focused editable cell/control). This is accurate —
    // unlike a raw DOM activeElement probe, which misfires on Univer because it
    // keeps a hidden input focused for keystroke capture even when the user is
    // merely selecting a cell. That misfire used to freeze remote real-time sync
    // the moment the user clicked the grid (visible symptom: cell edits from
    // peers taking many seconds / appearing stuck, while awareness-driven row
    // highlights stayed instant). With the precise signal, sync only pauses
    // during a genuine edit and resumes the instant it ends.
    if (typeof grid.isEditing === "function") return grid.isEditing();
    // Legacy fallback (stale Univer build without edit events): DOM heuristic.
    // A merely-focused grid control (e.g. the column-filter funnel button, which
    // keeps focus inside the host after its panel closes) must NOT freeze sync.
    const host = document.getElementById("lm-grid-host");
    const ae = document.activeElement;
    if (host && ae && ae !== host && host.contains(ae)) {
      const tag = (ae.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || ae.isContentEditable) return true;
    }
    return false;
  }

  // Fetch every row of the project (the grid paginates by テスト区分 client-side,
  // so it needs the full dataset). Rows are pulled in chunks to respect the
  // server's page-size cap, then concatenated.
  // Raw REST read straight from the DB, regardless of collaboration state. Used
  // both as the classic (non-collab) source and to pull freshly-imported rows so
  // they can be reconciled into the Y.Doc (see resyncSheetFromDb).
  async function fetchDbItems(sheet) {
    const acc = [];
    let p = 1;
    for (;;) {
      const params = { page: p, page_size: FETCH_CHUNK, sheet: sheet };
      if (quick) params.q = quick;
      const data = await LMApi.listItems(pid, params);
      const batch = data.items || [];
      acc.push.apply(acc, batch);
      if (acc.length >= (data.total || 0) || batch.length === 0) break;
      p++;
      if (p > 2000) break; // hard safety valve
    }
    return acc;
  }

  async function fetchAllItems(sheetKey) {
    const sheet = sheetKey || currentSheet;
    // When collaboration is live the shared Y.Doc — not REST — is the source of
    // truth (the server materializer is one-way Y→DB, so a REST read could show
    // rows the doc hasn't yet, or that a pending materialize will soft-delete).
    if (collabActive()) return collab.getItems(sheet);
    return fetchDbItems(sheet);
  }

  // After a server-side import while collaboration is live, the new rows exist
  // only in the DB. Pull them and reconcile them into the shared Y.Doc (keyed by
  // uuid) so every peer sees them and the materializer won't soft-delete them.
  async function resyncSheetFromDb(sheet) {
    if (!collabActive()) return;
    const savedQuick = quick;
    quick = "";                       // import result is the full sheet, unfiltered
    try {
      const dbItems = await fetchDbItems(sheet);
      collab.reconcileFromDb(sheet, dbItems);
      sheetItems[sheet] = collab.getItems(sheet);
    } finally {
      quick = savedQuick;
    }
  }

  // Group all rows into ordered pages, one per distinct テスト区分 (category),
  // preserving first-seen order. Rows without a category collapse into "未分类".
  function buildCategoryPages(items, sheetKey) {
    // Category (テスト区分) paging applies to the test sheet under BOTH engines:
    // the test worksheet shows one 区分 at a time, navigated by the external
    // pager. const/lib are flat (a single all-rows page).
    const key = sheetKey || currentSheet;
    if (key !== "test") {
      return [{ key: "__all__", label: key, items: items }];
    }
    const order = [];
    const buckets = new Map();
    items.forEach((it) => {
      const raw = it.category;
      const key = (raw === null || raw === undefined || raw === "") ? "__none__" : String(raw);
      if (!buckets.has(key)) { buckets.set(key, []); order.push(key); }
      buckets.get(key).push(it);
    });
    return order.map((key) => {
      const list = buckets.get(key);
      const cname = list[0].category_name;
      let label;
      if (key === "__none__") label = "未分类";
      else label = "区分 " + key + (cname ? " · " + cname : "");
      return { key: key, label: label, items: list };
    });
  }

  // Render the active sheet (from the sheetItems cache) into the grid. The test
  // sheet is paginated by テスト区分 — only the current category page is pushed
  // to the grid and the external pager is shown; const/lib render flat with no
  // pager. Engine-aware: Univer uses setSheet*, the fallback grid uses set*.
  // `incremental` (real-time collab remote sync only): when the row set is
  // unchanged structurally, push just the changed cells so the user's scroll,
  // selection and any active edit elsewhere survive. The grid auto-falls-back to
  // a full render whenever the structure differs, so passing it is always safe.
  function renderView(incremental) {
    const items = sheetItems[currentSheet] || [];
    total = items.length;
    let shown = items;
    if (currentSheet === "test") {
      categoryPages = buildCategoryPages(items, "test");
      if (page > categoryPages.length) page = categoryPages.length;
      if (page < 1) page = 1;
      const cur = categoryPages[page - 1];
      shown = cur ? cur.items : [];
    } else {
      categoryPages = buildCategoryPages(items, currentSheet);
    }
    pushFields(currentSheet, sheetFields[currentSheet] || fields);
    pushData(currentSheet, shown, incremental);
    document.getElementById("lm-count").textContent = `共 ${total} 条`;
    if (currentSheet === "test") renderPager();
    else document.getElementById("lm-pager").innerHTML = "";
    applyCollabPresence();         // repaint drops overlay/highlight → re-apply (§6.1)
    applyCellErrors();             // repaint drops cell error marks → re-apply (§12.2)
  }

  // Re-push the current sheet's rows to the grid after the engine settles, to
  // recover from Univer builds that swallow setValues issued before the workbook
  // has rendered (visible symptom: correct row count but empty cells). Runs a few
  // times on animation frames + short timeouts; each pass is a cheap idempotent
  // re-render, so a working build simply repaints identical data.
  function scheduleRepaint() {
    let passes = 0;
    const tick = () => {
      try { renderView(); } catch (_e) { /* best-effort repaint */ }
      if (++passes < 3) setTimeout(tick, 250);
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(tick);
    else setTimeout(tick, 0);
  }

  async function silentPoll() {
    if (refreshUnsafe()) return;
    try {
      const items = await fetchAllItems();
      const sig = dataSignature(items, items.length);
      if (sig === lastSig) return;            // nothing changed → no re-render
      if (refreshUnsafe()) return;            // re-check after the await
      lastSig = sig;
      sheetItems[currentSheet] = items;
      renderView();
      toast("表格已同步他人的最新修改", true);
    } catch (_e) { /* polling is silent: never surface transient errors */ }
  }

  function startPolling() {
    // Real-time collab pushes changes live over the Y.Doc, so the 8s REST poll
    // is redundant (and would fight the CRDT as source of truth) — skip it.
    if (collabActive()) return;
    if (pollTimer) return;
    pollTimer = setInterval(silentPoll, POLL_MS);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function toast(msg, ok) {
    const t = document.getElementById("lm-toast");
    t.textContent = msg;
    t.className = "lm-toast " + (ok ? "lm-ok" : "lm-err");
    t.hidden = false;
    setTimeout(() => { t.hidden = true; }, 3200);
  }
  window.LMToast = toast;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  async function saveCell(item, changes) {
    if (collabActive()) {
      // CRDT merge — no version check, no soft conflict. Returns the merged row.
      return collab.setCell(currentSheet, item, changes);
    }
    savingCount++;
    try {
      const data = await LMApi.patchItem(pid, item.id, item.version, changes);
      return data.item;
    } finally {
      savingCount--;
    }
  }

  async function loadProject() {
    const data = await LMApi.getProject(pid);
    const p = data.project;
    document.getElementById("lm-proj-title").textContent = `${p.code} · ${p.name}`;
    const st = document.getElementById("lm-proj-status");
    st.textContent = p.status;
    st.className = "lm-badge lm-status-" + p.status;
  }

  async function loadFields() {
    const data = await LMApi.listFields(pid, { sheet: currentSheet });
    fields = data.fields;
    sheetFields[currentSheet] = fields;
    pushFields(currentSheet, fields);
  }

  async function loadItems() {
    const items = await fetchAllItems();
    sheetItems[currentSheet] = items;
    lastSig = dataSignature(items, items.length);
    document.getElementById("lm-grid-engine").textContent =
      grid.engine === "univer" ? "· Univer 引擎" : "· 内置表格";
    renderView();
  }

  // Native-multi-sheet Univer: load field set + all rows for every worksheet up
  // front so switching tabs is instant. Called once at init for the Univer engine.
  async function loadAllUniverSheets() {
    for (const key of SHEET_KEYS) {
      const fd = await LMApi.listFields(pid, { sheet: key });
      sheetFields[key] = fd.fields;
      pushFields(key, fd.fields);
      const items = await fetchAllItems(key);
      sheetItems[key] = items;
      // The test worksheet shows one テスト区分 page at a time; const/lib flat.
      if (key === "test") {
        const pages = buildCategoryPages(items, "test");
        pushData("test", pages.length ? pages[0].items : items);
      } else {
        pushData(key, items);
      }
      if (key === currentSheet) {
        lastSig = dataSignature(items, items.length);
        fields = fd.fields;
      }
    }
    document.getElementById("lm-grid-engine").textContent = "· Univer 引擎";
    // Sync the count + category pager for the initially active sheet.
    page = 1;
    renderView();
  }

  // Pager navigates テスト区分 pages (not fixed-size pages): each button jumps to
  // the previous/next category; the label shows the current category and its size.
  function renderPager() {
    const pages = categoryPages.length;
    const el = document.getElementById("lm-pager");
    el.innerHTML = "";
    if (pages <= 1) return;
    const mk = (label, target, disabled) => {
      const b = document.createElement("button");
      b.className = "lm-btn lm-btn-sm";
      b.textContent = label;
      b.disabled = disabled;
      b.addEventListener("click", () => { page = target; renderView(); });
      return b;
    };
    el.appendChild(mk("上一区分", page - 1, page <= 1));
    const cur = categoryPages[page - 1];
    const span = document.createElement("span");
    span.className = "lm-muted";
    span.textContent = ` ${cur ? cur.label : ""}（${page} / ${pages}，${cur ? cur.items.length : 0} 条） `;
    el.appendChild(span);
    el.appendChild(mk("下一区分", page + 1, page >= pages));
  }

  // --- Toolbar ---
  // Draft row: server auto-generates case_id and applies field defaults;
  // required cells may stay blank and be filled inline afterwards.
  async function addRow(opts) {
    try {
      let created = null;
      if (collabActive()) {
        created = collab.insertRow(currentSheet, {
          anchorId: opts && opts.anchor_id, place: opts && opts.place, values: {},
        });
      } else {
        const data = await LMApi.createItem(
          pid, { draft: true }, Object.assign({ sheet: currentSheet }, opts || {}));
        created = data && data.item;
      }
      await loadItems();
      focusItemPage(created);
      toast(opts && opts.anchor_id ? "已插入空白行" : "已新增空白行，可直接编辑", true);
    } catch (ex) { toast(ex.message, false); }
  }

  // After inserting a row, make sure it is actually on screen. The test sheet is
  // paginated by テスト区分 (category), so a freshly inserted row can land on a
  // category page other than the one being viewed — the total count would rise
  // while the row itself stays hidden (visible symptom: "行数增加了但空白行不显
  // 示"). Jump the pager to the page holding the new row (matched by uuid/id) and
  // re-render. No-op for flat sheets (const/lib) and when already on that page.
  function focusItemPage(item) {
    if (!item || currentSheet !== "test") return;
    const uuid = item.uuid;
    const idx = categoryPages.findIndex((p) => (p.items || []).some(
      (it) => (uuid && it.uuid === uuid) || (item.id != null && it.id === item.id)));
    if (idx >= 0 && idx + 1 !== page) {
      page = idx + 1;
      renderView();
    }
  }

  // Insert above/below relative to the current selection (or the pointed row
  // from the context menu). Falls back to append when nothing is selected.
  async function insertRowAt(item, where) {
    const ids = grid.getSelectedIds();
    let anchor = item ? item.id : null;
    if (!anchor && ids.length) anchor = where === "above" ? ids[0] : ids[ids.length - 1];
    if (!anchor) return addRow();
    return addRow({ anchor_id: anchor, place: where });
  }

  async function bulkDuplicate(ids) {
    const list = ids && ids.length ? ids : grid.getSelectedIds();
    if (!list.length) { toast("请先勾选要复制的行", false); return; }
    try {
      let created;
      if (collabActive()) created = collab.duplicateRows(currentSheet, list);
      else created = (await LMApi.bulkDuplicateItems(pid, list)).created;
      await loadItems();
      toast(`已复制 ${created} 行`, true);
    } catch (ex) { toast(ex.message, false); }
  }

  async function bulkDelete(ids) {
    const list = ids && ids.length ? ids : grid.getSelectedIds();
    if (!list.length) { toast("请先勾选要删除的行", false); return; }
    if (!confirm(`确定删除所选 ${list.length} 行？`)) return;
    try {
      let deleted;
      if (collabActive()) deleted = collab.deleteRows(currentSheet, list);
      else deleted = (await LMApi.bulkDeleteItems(pid, list)).deleted;
      await loadItems();
      toast(`已删除 ${deleted} 行`, true);
    } catch (ex) { toast(ex.message, false); }
  }

  async function moveRows(ids, dir) {
    const list = ids && ids.length ? ids : grid.getSelectedIds();
    if (!list.length) { toast("请先勾选要移动的行", false); return; }
    try {
      if (collabActive()) collab.moveRows(currentSheet, list, dir);
      else await LMApi.moveItems(pid, list, dir);
      const keep = list.slice();
      await loadItems();
      // Re-apply the selection so the user can keep nudging the same block.
      keep.forEach((id) => grid._setRowSelected(id, true));
      grid._syncSelectAll();
      grid._emitSelection();
    } catch (ex) { toast(ex.message, false); }
  }

  // Selection changed: publish the local editing cursor into awareness so peers
  // can highlight the row this user is on (Phase 2, design §6.1). The row is
  // located by its stable ``uuid`` — never an absolute row number — so it maps
  // correctly even when peers are filtering/sorting. Falsy uuid clears it.
  function updateSelectionUI(ids) {
    if (!collabActive() || !collab.setLocalSelection) return;
    const items = sheetItems[currentSheet] || [];
    const uuidOf = {};
    items.forEach((it) => { if (it.uuid) uuidOf[Number(it.id)] = it.uuid; });

    let sel = null;
    if (grid && typeof grid.getActiveSelection === "function") {
      sel = grid.getActiveSelection();
    }
    // Fallback: a bare id list with no active-cell info.
    if (!sel) {
      const rowIds = (ids || []).map(Number);
      sel = { anchor: null, rowIds: rowIds, cols: null };
    }
    // Map selected row ids -> stable uuids (rows without a uuid are dropped).
    const rowsUuid = [];
    (sel.rowIds || []).forEach((id) => {
      const u = uuidOf[Number(id)];
      if (u) rowsUuid.push(u);
    });
    // Map the anchor id -> uuid so peers can place the active-cell box + label.
    let anchor = null;
    if (sel.anchor && sel.anchor.id != null) {
      const u = uuidOf[Number(sel.anchor.id)];
      if (u) anchor = { uuid: u, col: (sel.anchor.col == null ? 0 : sel.anchor.col) };
    }
    collab.setLocalSelection(currentSheet, anchor, rowsUuid, sel.cols || null);
  }

  // Render remote collaborators' SELECTIONS for the active sheet (design §6.1):
  // each peer's selection is drawn as a coloured BORDER (anchor cell + selected
  // rows) with a Figma-style name tag. Both grids draw a zero-mutation overlay:
  // the fallback grid positions DOM boxes over the table; the Univer engine draws
  // a transparent border layer above the canvas (no cell-style pollution). Peer
  // row uuids are mapped to the ids that exist in THIS view, so a differing
  // sort/filter never mis-places a box. Called on every selection change and
  // after each render (a full repaint drops the overlay, so it must be re-applied).
  function applyCollabPresence() {
    if (!grid) return;
    if (typeof grid.setRemoteSelections !== "function") {
      // A grid without the selection-overlay API is almost always a STALE Univer
      // bundle built before the border-overlay work. The source is correct but
      // app/static/vendor/univer/univer.full.umd.js must be rebuilt
      // (cd frontend && npm run build). Warn once so this never wastes debugging.
      if (!applyCollabPresence._warned) {
        applyCollabPresence._warned = true;
        try {
          console.warn("[lanmatrix] Remote selections disabled: the active grid " +
            "has no setRemoteSelections(). Rebuild the Univer bundle " +
            "(cd frontend && npm run build) so the overlay adapter is included.");
        } catch (_e) { /* noop */ }
      }
      return;
    }
    const list = (remoteSelections && remoteSelections[currentSheet]) || [];
    const items = sheetItems[currentSheet] || [];
    const idByUuid = {};
    items.forEach((it) => { if (it.uuid) idByUuid[it.uuid] = it.id; });
    const peers = [];
    list.forEach((p) => {
      // Map this peer's row uuids -> ids present in my view (others dropped).
      const rowIds = [];
      (p.rows || []).forEach((u) => {
        const id = idByUuid[u];
        if (id !== undefined) rowIds.push(id);
      });
      let anchor = null;
      if (p.anchor && p.anchor.uuid) {
        const id = idByUuid[p.anchor.uuid];
        if (id !== undefined) anchor = { id: id, col: (p.anchor.col == null ? 0 : p.anchor.col) };
      }
      if (!rowIds.length && !anchor) return;   // nothing of this peer is visible
      peers.push({ key: p.key, name: p.name, color: p.color,
        anchor: anchor, rowIds: rowIds, cols: p.cols || null });
    });
    try {
      if (window.LM_DEBUG_CURSOR) {
        console.log("[editor] applyCollabPresence sheet:", currentSheet,
          "list:", list.length, "peers:", peers.length, peers);
      }
    } catch (_e) { /* noop */ }
    try { grid.setRemoteSelections(peers); } catch (_e) { /* best-effort */ }
  }

  // Paint cells the server rejected during materialization red (design §12.2).
  // The materializer publishes an authoritative uuid->{cells,message} snapshot on
  // the shared Y.Map, so a row that gets fixed clears itself. We translate uuid
  // to the current row id for the active sheet and hand the grid a
  // { rowId: { cells:[field_key], message } } map; a full repaint drops the marks
  // so this is re-applied after every render.
  function applyCellErrors() {
    if (!grid || typeof grid.setCellErrors !== "function") return;
    const items = sheetItems[currentSheet] || [];
    const idByUuid = {};
    items.forEach((it) => { if (it.uuid) idByUuid[it.uuid] = it.id; });
    const map = {};
    Object.keys(remoteRowErrors || {}).forEach((uuid) => {
      const err = remoteRowErrors[uuid];
      if (err && err.sheet && err.sheet !== currentSheet) return;
      const id = idByUuid[uuid];
      if (id === undefined) return;
      map[id] = { cells: (err.cells || []).slice(), message: err.message || "" };
    });
    try { grid.setCellErrors(map); } catch (_e) { /* best-effort */ }
  }

  // --- Sheet switching (test / const / lib) ---
  // With the rebuilt Univer bundle each sheet is a real worksheet tab and
  // switching is driven natively via ``onSheetChange``. When the bundle lacks
  // native tabs (stale Univer or the builtin grid), a fallback tab bar
  // (``#lm-sheet-tabs``) is shown so const/lib stay reachable.
  // ``applySheetContext`` syncs editor-side state to the active sheet.
  async function applySheetContext(sheet, reload) {
    currentSheet = sheet;
    page = 1;
    fields = sheetFields[sheet] || fields;
    if (reload) {
      await loadFields();
      await loadItems();
    } else {
      // Univer tab switch: re-fetch this worksheet's fields + rows from the
      // server so the grid always reflects the database, never a stale in-memory
      // cache (const/lib rows that were imported after page load must reappear).
      try {
        const fd = await LMApi.listFields(pid, { sheet });
        sheetFields[sheet] = fd.fields;
        fields = fd.fields;
        const items = await fetchAllItems(sheet);
        sheetItems[sheet] = items;
        lastSig = dataSignature(items, items.length);
      } catch (_e) {
        // Transient network error: fall back to the cache so the tab still shows.
      }
      renderView();
    }
    syncFallbackTabs();
    syncRunButton();
  }

  // The "队列测试" (run-selected) button only applies to the test sheet, whose
  // rows carry a ``test_id`` — the unique key used to enqueue JSON-runner jobs.
  const runSelectedBtn = document.getElementById("lm-run-selected");
  function syncRunButton() {
    if (!runSelectedBtn) return;
    runSelectedBtn.hidden = currentSheet !== "test";
  }
  if (runSelectedBtn) {
    runSelectedBtn.addEventListener("click", async () => {
      const ids = (grid && grid.getSelectedIds) ? grid.getSelectedIds() : [];
      if (!ids || !ids.length) {
        toast("请先在测试用例表中选择要入队的行", false);
        return;
      }
      const byId = {};
      (sheetItems["test"] || []).forEach((it) => { byId[it.id] = it; });
      const testIds = [];
      const noId = [];
      ids.forEach((rid) => {
        const it = byId[rid];
        const tid = it && (it.test_id != null ? String(it.test_id).trim() : "");
        if (tid) { if (testIds.indexOf(tid) < 0) testIds.push(tid); }
        else noId.push(rid);
      });
      if (!testIds.length) {
        toast("所选行缺少 test_id，无法入队", false);
        return;
      }
      runSelectedBtn.disabled = true;
      try {
        const model = (window.LMDefaultModel || "").trim();
        const payload = { test_ids: testIds };
        if (model) payload.model = model;
        const res = await LMApi.runSelectedTasks(pid, payload);
        const created = (res.created || []).length;
        const missing = (res.missing || []);
        const errors = (res.errors || []);
        let msg = `已入队 ${created} 个测试`;
        if (missing.length) msg += `；缺失 ${missing.length}`;
        if (errors.length) msg += `；失败 ${errors.length}`;
        if (noId.length) msg += `；${noId.length} 行无 test_id`;
        toast(msg, errors.length === 0 && missing.length === 0);
      } catch (ex) {
        toast(ex.message || "入队失败", false);
      } finally {
        runSelectedBtn.disabled = false;
      }
    });
  }

  // Fallback worksheet tab bar — rendered only when the grid has no native
  // Univer tabs. Clicking a tab reloads that sheet's fields + rows into the
  // single grid so test/const/lib remain reachable on a stale/builtin bundle.
  const sheetTabsEl = document.getElementById("lm-sheet-tabs");
  function buildFallbackTabs() {
    if (!sheetTabsEl || hasNativeSheets()) return;
    sheetTabsEl.hidden = false;
    sheetTabsEl.innerHTML = "";
    SHEET_SPECS.forEach((s) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "lm-sheet-tab" + (s.key === currentSheet ? " active" : "");
      b.textContent = s.name;
      b.dataset.sheet = s.key;
      b.addEventListener("click", () => {
        if (s.key === currentSheet) return;
        applySheetContext(s.key, true).catch((ex) => toast(ex.message, false));
      });
      sheetTabsEl.appendChild(b);
    });
  }
  function syncFallbackTabs() {
    if (!sheetTabsEl || sheetTabsEl.hidden) return;
    sheetTabsEl.querySelectorAll(".lm-sheet-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.sheet === currentSheet);
    });
  }

  // --- Export dialog (unifies generic + Japanese test-matrix export) ---
  const exportDialog = document.getElementById("lm-export-dialog");
  const exportFormatSel = document.getElementById("lm-export-format");
  document.getElementById("lm-export").addEventListener("click", () => {
    document.getElementById("lm-export-error").hidden = true;
    exportDialog.showModal();
  });

  document.getElementById("lm-export-ok").addEventListener("click", async (e) => {
    e.preventDefault();
    const err = document.getElementById("lm-export-error");
    err.hidden = true;
    const format = exportFormatSel ? exportFormatSel.value : "generic";
    try {
      if (format === "test_matrix" || format === "libfunc" || format === "const") {
        const urlFor = {
          test_matrix: LMApi.testMatrixExportUrl,
          libfunc: LMApi.libFuncExportUrl,
          const: LMApi.constExportUrl,
        }[format];
        const a = document.createElement("a");
        a.href = urlFor(pid);
        a.click();
      } else {
        const blob = await LMApi.exportProject(pid, {});
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `project_${pid}_export.xlsx`;
        a.click();
        URL.revokeObjectURL(url);
      }
      exportDialog.close();
    } catch (ex) {
      err.textContent = ex.message; err.hidden = false;
    }
  });

  // --- Import dialog ---
  const importDialog = document.getElementById("lm-import-dialog");
  const importFormatSel = document.getElementById("lm-import-format");
  let pendingJob = null;   // generic two-step preview job
  let pendingTMFile = null; // one-step file (test_matrix / libfunc / const)
  let pendingImportFormat = null; // which one-step format the file belongs to

  // One-step formats parse straight to create/update (no preview job), keyed by
  // the API call and the label shown in the confirmation prompt.
  const ONE_STEP = {
    test_matrix: { call: (id, f, m) => LMApi.importTestMatrix(id, f, m),
                   label: "日文测试矩阵表头" },
    libfunc: { call: (id, f, m) => LMApi.importLibFunc(id, f, m),
               label: "Lib 函数库 (lib_Func + 手順明细)" },
    const: { call: (id, f, m) => LMApi.importConst(id, f, m),
             label: "Const 常量表 (No. / 識別子名)" },
  };

  function applyFormatUI() {
    const oneStep = importFormatSel && importFormatSel.value !== "generic";
    document.querySelectorAll("#lm-import-mode option[data-generic-only]")
      .forEach((o) => { o.hidden = oneStep; o.disabled = oneStep; });
    if (oneStep) {
      const m = document.getElementById("lm-import-mode");
      if (m.selectedOptions[0] && m.selectedOptions[0].hidden) m.value = "upsert";
    }
  }
  if (importFormatSel) importFormatSel.addEventListener("change", () => {
    applyFormatUI();
    document.getElementById("lm-import-summary").innerHTML = "";
    document.getElementById("lm-import-commit").disabled = true;
    document.getElementById("lm-import-dialog-file").value = "";
    pendingJob = null; pendingTMFile = null; pendingImportFormat = null;
  });

  // Refresh the Test-Matrix destructive-replace warning when the mode changes
  // after a file was already picked (the generic path re-previews on file change
  // instead, so it only needs the warning re-rendered for the one-step TM flow).
  const importModeSel = document.getElementById("lm-import-mode");
  if (importModeSel) importModeSel.addEventListener("change", () => {
    if (!pendingTMFile) return;
    const mode = importModeSel.value;
    const warn = mode === "replace_all"
      ? `<p class="lm-error"><strong>整表替换：</strong>确认后将先删除本项目现有全部测试项，再导入该文件的全部行。此操作不可撤销（旧数据进入回收站）。</p>`
      : "";
    const label = (ONE_STEP[pendingImportFormat] || {}).label || "该格式";
    document.getElementById("lm-import-summary").innerHTML =
      `<p>已选择《${esc(pendingTMFile.name)}》。将按${esc(label)}解析并生成/更新测试项，点“确认导入”继续。</p>` + warn;
  });

  document.getElementById("lm-import").addEventListener("click", () => {
    document.getElementById("lm-import-summary").innerHTML = "";
    document.getElementById("lm-import-error").hidden = true;
    document.getElementById("lm-import-commit").disabled = true;
    document.getElementById("lm-import-dialog-file").value = "";
    pendingJob = null;
    pendingTMFile = null;
    // Preselect the import format that matches the active sheet.
    if (importFormatSel) {
      const pref = currentSheet === "const" ? "const"
        : currentSheet === "lib" ? "libfunc" : importFormatSel.value;
      importFormatSel.value = pref;
    }
    applyFormatUI();
    importDialog.showModal();
  });

  document.getElementById("lm-import-dialog-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const err = document.getElementById("lm-import-error");
    err.hidden = true;
    const format = importFormatSel ? importFormatSel.value : "generic";
    if (ONE_STEP[format]) {
      pendingTMFile = file;
      pendingImportFormat = format;
      pendingJob = null;
      const mode = document.getElementById("lm-import-mode").value;
      const warn = mode === "replace_all"
        ? `<p class="lm-error"><strong>整表替换：</strong>确认后将先删除本项目现有全部测试项，再导入该文件的全部行。此操作不可撤销（旧数据进入回收站）。</p>`
        : "";
      document.getElementById("lm-import-summary").innerHTML =
        `<p>已选择《${esc(file.name)}》。将按${esc(ONE_STEP[format].label)}解析并生成/更新测试项，点“确认导入”继续。</p>` + warn;
      document.getElementById("lm-import-commit").disabled = false;
      return;
    }
    try {
      const mode = document.getElementById("lm-import-mode").value;
      const data = await LMApi.createImport(pid, file, mode);
      pendingJob = data.job;
      pendingTMFile = null;
      const pv = pendingJob.preview || {};
      const errRows = (pv.errors || []).slice(0, 20).map((x) =>
        `<tr><td>${x.row}</td><td>${esc(x.column)}</td><td>${esc(x.message)}</td></tr>`).join("");
      document.getElementById("lm-import-summary").innerHTML =
        `<p>共 ${pv.total} 行：新增 ${pv.insert}，更新 ${pv.update}，错误 ${pv.invalid}。</p>` +
        (errRows ? `<table class="lm-table lm-preview"><thead><tr><th>行</th><th>列</th><th>问题</th></tr></thead><tbody>${errRows}</tbody></table>` : "");
      document.getElementById("lm-import-commit").disabled =
        !(pv.invalid === 0 || (pendingJob.parameters && pendingJob.parameters.mode === "replace_all"));
    } catch (ex) {
      err.textContent = ex.message; err.hidden = false;
    }
  });

  document.getElementById("lm-import-commit").addEventListener("click", async (e) => {
    e.preventDefault();
    const err = document.getElementById("lm-import-error");
    err.hidden = true;
    const commitBtn = document.getElementById("lm-import-commit");
    commitBtn.disabled = true;
    try {
      if (pendingTMFile) {
        const mode = document.getElementById("lm-import-mode").value;
        const spec = ONE_STEP[pendingImportFormat] || ONE_STEP.test_matrix;
        const data = await spec.call(pid, pendingTMFile, mode);
        const s = data.summary || {};
        const rowErrors = s.errors || [];
        // Jump to the sheet the imported rows landed on so they're visible.
        const targetSheet = pendingImportFormat === "libfunc" ? "lib"
          : pendingImportFormat === "const" ? "const" : "test";
        if (targetSheet !== currentSheet) {
          if (isUniver() && typeof grid.setActiveSheetKey === "function") {
            grid.setActiveSheetKey(targetSheet);
          }
          await applySheetContext(targetSheet, false);
        }
        await loadFields();
        // Collab: fold the imported DB rows into the shared Y.Doc first, else
        // loadItems (reading the Y.Doc) shows nothing and they get materialized
        // away. No-op when collaboration is inactive.
        if (collabActive()) await resyncSheetFromDb(targetSheet);
        await loadItems();
        if (rowErrors.length) {
          // Keep the dialog open and list every failed row (行号 + case_id + 消息),
          // grouped by identical cause so common failure reasons stand out.
          const byMsg = new Map();
          rowErrors.forEach((er) => {
            const m = er.message || "未知错误";
            if (!byMsg.has(m)) byMsg.set(m, []);
            byMsg.get(m).push(er);
          });
          // Most-common cause first.
          const groups = [...byMsg.entries()]
            .sort((a, b) => b[1].length - a[1].length)
            .map(([m, ers]) => {
              const rowsHtml = ers
                .slice()
                .sort((a, b) => (a.row || 0) - (b.row || 0))
                .map((er) =>
                  `<li>第 ${esc(er.row)} 行 · <code>${esc(er.case_id || "-")}</code></li>`)
                .join("");
              return `<li class="lm-import-errgroup"><b>${esc(m)}</b>` +
                `<span class="lm-muted">（${ers.length} 行）</span>` +
                `<ul class="lm-import-errrows">${rowsHtml}</ul></li>`;
            }).join("");
          // One-line summary of causes so the shared reason is obvious at a glance.
          const summaryLine = [...byMsg.entries()]
            .sort((a, b) => b[1].length - a[1].length)
            .map(([m, ers]) => `${esc(m)}（${ers.length}）`)
            .join("；");
          const delPart = s.deleted ? `删除 ${s.deleted}，` : "";
          document.getElementById("lm-import-summary").innerHTML =
            `<p>导入完成：${delPart}新增 ${s.created}，更新 ${s.updated}，` +
            `<b style="color:var(--danger,#c0392b)">失败 ${rowErrors.length}</b>。</p>` +
            `<p class="lm-muted">失败原因归类：${summaryLine}</p>` +
            `<ul class="lm-import-errlist">${groups}</ul>`;
          document.getElementById("lm-import-commit").disabled = true;
          toast(`导入完成，但有 ${rowErrors.length} 行失败，请查看失败原因`, false);
          return;
        }
        importDialog.close();
        toast(
          `导入完成：${s.deleted ? `删除 ${s.deleted}，` : ""}` +
          `新增 ${s.created}，更新 ${s.updated}`, true);
        return;
      }
      if (!pendingJob) return;
      const r = await LMApi.commitImport(pendingJob.id);
      importDialog.close();
      if (collabActive()) await resyncSheetFromDb(currentSheet);
      await loadItems();
      toast(`导入完成：新增 ${r.inserted}，更新 ${r.updated}`, true);
    } catch (ex) {
      err.textContent = ex.message; err.hidden = false;
      commitBtn.disabled = false;
    }
  });

  // Compose the collaboration badge from BOTH the socket state and the live peer
  // count, so a transient disconnect stays visible instead of being masked by a
  // stale "在线 N 人" presence label. While offline the editor keeps writing to
  // the local Y.Doc (offline editing) and y-websocket auto-reconnects; the label
  // reassures the user that pending changes will sync once the link is restored.
  // We never fall back to the REST path once collaboration has started.
  let collabConn = "connecting";     // "connecting" | "connected" | "disconnected"
  let collabPeers = 0;
  let collabEverConnected = false;
  let collabMembers = [];            // last online-member roster (design §6.1)
  let remoteSelections = {};         // { sheet: [ {key,name,color,anchor,rows,cols} ] } (§6.1)
  let remoteRowErrors = {};          // { uuid: {cells:[field_key], message, sheet} } (§12.2)
  function renderCollabStatus() {
    const el = document.getElementById("lm-collab-status");
    if (!el) return;
    let text, cls;
    if (collabConn === "connected") {
      collabEverConnected = true;
      text = collabPeers > 0 ? `实时协同 · 在线 ${collabPeers} 人` : "实时协同 · 已连接";
      cls = "lm-collab-on";
    } else if (!collabEverConnected) {
      text = "实时协同 · 连接中…";
      cls = "lm-collab-wait";
    } else {
      // Dropped after a good connection: stay on the Y.Doc (offline editing) and
      // let y-websocket keep reconnecting. Make the outage unmistakable.
      text = "实时协同 · 已断开，重连中…（离线编辑，恢复后自动同步）";
      cls = "lm-collab-off";
    }
    el.textContent = text;
    el.className = "lm-badge lm-collab-badge " + cls;
    el.hidden = false;
  }
  function setCollabStatus(text) {
    const el = document.getElementById("lm-collab-status");
    if (el) { el.textContent = text; el.hidden = false; }
  }

  // Online-member list (Phase 2, design §6.1). Pure awareness read: draw one
  // colour-coded avatar chip per collaborator (deduped by user.id upstream),
  // newest presence reflected live. The local user is flagged with「（我）」and a
  // ring; a peer with multiple open tabs shows a small connection count. The
  // chip initials come from the user's display name. Hidden entirely when no one
  // (not even self) is present, e.g. before the socket has synced.
  function avatarInitials(name) {
    const s = String(name || "").trim();
    if (!s) return "?";
    // CJK: first glyph reads well; latin: up to two leading word initials.
    if (/[\u4e00-\u9fa5]/.test(s[0])) return s.slice(0, 1);
    const parts = s.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return s.slice(0, 2).toUpperCase();
  }
  function renderMembers(users) {
    const box = document.getElementById("lm-collab-members");
    if (!box) return;
    const list = users || [];
    if (!list.length || collabConn !== "connected") { box.hidden = true; box.innerHTML = ""; return; }
    box.innerHTML = "";
    list.forEach((u) => {
      const chip = document.createElement("span");
      chip.className = "lm-collab-avatar" + (u.self ? " lm-collab-avatar-self" : "");
      chip.style.background = u.color || "#888";
      chip.textContent = avatarInitials(u.name);
      const who = u.self ? `${u.name}（我）` : u.name;
      chip.title = u.conns > 1 ? `${who} · ${u.conns} 个连接` : who;
      if (u.conns > 1) {
        const b = document.createElement("i");
        b.className = "lm-collab-avatar-badge";
        b.textContent = u.conns > 9 ? "9+" : String(u.conns);
        chip.appendChild(b);
      }
      box.appendChild(chip);
    });
    box.hidden = false;
  }

  // Apply a remote Y.Doc change to the active sheet (design §1.3). If the user is
  // mid-edit (refreshUnsafe), the apply is deferred and retried on a short timer
  // so a peer's concurrent edit to a DIFFERENT cell still shows up as soon as the
  // user goes idle — instead of waiting for their next unrelated action.
  let remoteRetryTimer = null;
  function syncFromCollab(sheet) {
    if (!collabActive()) return;
    if (sheet && sheet !== currentSheet) return;   // other tab: it reloads on switch
    if (refreshUnsafe()) {
      if (remoteRetryTimer) return;
      remoteRetryTimer = setInterval(() => {
        if (!collabActive()) { clearInterval(remoteRetryTimer); remoteRetryTimer = null; return; }
        if (refreshUnsafe()) return;                // still busy → keep waiting
        clearInterval(remoteRetryTimer); remoteRetryTimer = null;
        sheetItems[currentSheet] = collab.getItems(currentSheet);
        renderView(true);
      }, 600);
      return;
    }
    sheetItems[currentSheet] = collab.getItems(currentSheet);
    renderView(true);              // incremental cell-level apply
  }

  // Try to bring up real-time collaboration. Strictly optional: it needs the
  // vendored Yjs bundle, the server flag, an item.edit token AND a reachable
  // collab socket that syncs — any missing piece leaves the editor on its
  // classic REST + polling path, unchanged.
  async function tryStartCollab() {
    if (!collabAvailable) return;
    if (!window.LMCollabController || !window.LMCollab) return;
    try {
      const ctrl = window.LMCollabController.create({
        pid, api: LMApi, sheets: SHEET_KEYS, rowPrefix: ROW_ARRAY_PREFIX,
      });
      const active = await ctrl.start({
        onChange: (sheet) => {
          if (!ctrl.isActive()) return;  // ignore events fired during initial sync
          syncFromCollab(sheet);         // §1.3: incremental, edit-safe apply
        },
        onStatus: (status) => {
          collabConn = status === "connected" ? "connected"
            : status === "connecting" ? "connecting" : "disconnected";
          renderCollabStatus();
          // Hide the roster while offline (its awareness is stale); it repopulates
          // from the next presence event once the socket resyncs.
          renderMembers(collabMembers);
        },
        onPresence: (users) => {
          collabMembers = users;
          collabPeers = users.length;
          renderCollabStatus();
          renderMembers(users);
        },
        onCursors: (bySheet) => {
          remoteSelections = bySheet || {};
          applyCollabPresence();
        },
        onRowErrors: (byUuid) => {
          remoteRowErrors = byUuid || {};
          applyCellErrors();
        },
      });
      if (active) {
        collab = ctrl;
        collabConn = "connected";
        renderCollabStatus();
        toast("已连接实时协同，多人编辑将实时同步", true);
      }
    } catch (_e) { /* stay on REST */ }
  }

  async function init() {
    await window.LMReady;
    await loadConfig();
    grid = LMGrid.create({
      host: document.getElementById("lm-grid-host"),
      fields,
      sheets: SHEET_SPECS,
      onSheetChange: (key) => {
        // User clicked a native Univer worksheet tab: sync editor state. Data is
        // already loaded in the worksheet, so no reload needed.
        applySheetContext(key, false).catch((ex) => toast(ex.message, false));
      },
      onSave: saveCell,
      onSelectionChange: updateSelectionUI,
      onInsert: (item, where) => insertRowAt(item, where),
      onBulkDuplicate: (ids) => bulkDuplicate(ids),
      onBulkDelete: (ids) => bulkDelete(ids),
      onMove: (ids, dir) => moveRows(ids, dir),
      onComment: async (item, key) => {
        const text = prompt("为该单元格添加评论：");
        if (text) {
          try { await LMApi.addComment(pid, item.id, key, text); toast("已添加评论", true); }
          catch (ex) { toast(ex.message, false); }
        }
      },
      onDelete: async (item) => {
        if (!confirm(`确定删除该行（${item.case_id || "#" + item.id}）？`)) return;
        try {
          if (collabActive()) collab.deleteRows(currentSheet, [item.id]);
          else await LMApi.deleteItem(pid, item.id);
          await loadItems();
          toast("已删除该行", true);
        } catch (ex) { toast(ex.message, false); }
      },
      onSteps: (item) => {
        if (!window.LMStepsEditor) { toast("步骤编辑器未加载", false); return; }
        const stepsKey = SHEET_STEPS[currentSheet];
        if (!stepsKey) { toast("当前 Sheet 无测试手顺字段", false); return; }
        // Enqueue + run-status are only meaningful for test-sheet rows, which
        // carry a ``test_id`` — the key used to enqueue JSON-runner jobs.
        const testId = (currentSheet === "test" && item && item.test_id != null)
          ? String(item.test_id).trim() : "";
        LMStepsEditor.open(item, {
          fieldKey: stepsKey,
          testId,
          // Live steps sub-structure CRDT binding (item 3): lets the drawer
          // observe remote edits and diff-write locally so concurrent step edits
          // merge instead of clobbering the whole JSON blob. Null in REST mode.
          live: (collabActive() && collab && typeof collab.getStepsMap === "function") ? {
            getMap: () => collab.getStepsMap(currentSheet, item, stepsKey),
            commit: (doc) => collab.commitSteps(currentSheet, item, stepsKey, doc),
            toDoc: (v) => collab.stepsDocFrom(v),
          } : null,
          // Lib/Const reference lookup: sourced from the CURRENT project's shared
          // Y.Doc when collab is live, else the DB. Missing sheets resolve to [].
          loadRef: async () => {
            const grab = async (sheet) => {
              try { return await fetchAllItems(sheet); } catch (_e) { return []; }
            };
            const [lib, cst] = await Promise.all([grab("lib"), grab("const")]);
            return { lib, const: cst };
          },
          onSave: async (json) => {
            const changes = {}; changes[stepsKey] = json;
            let merged;
            if (collabActive()) {
              merged = collab.setCell(currentSheet, item, changes);
            } else {
              const data = await LMApi.patchItem(pid, item.id, item.version, changes);
              merged = data.item;
            }
            item.version = merged.version;
            item[stepsKey] = merged[stepsKey];
            await loadItems();
            toast("步骤明细已保存", true);
          },
          onEnqueue: testId ? (async (tid) => {
            const payload = { test_ids: [tid] };
            const model = (window.LMDefaultModel || "").trim();
            if (model) payload.model = model;
            const res = await LMApi.runSelectedTasks(pid, payload);
            const errors = (res.errors || []);
            const missing = (res.missing || []);
            if (errors.length) throw new Error(errors[0].error || "入队失败");
            if (missing.length) throw new Error("未找到该 test_id 对应的测试行");
            toast(`已入队测试 ${tid}`, true);
            return res;
          }) : null,
          getStatus: testId ? (async (tid) => {
            const res = await LMApi.listProjectTasks(pid);
            const tasks = (res && res.tasks) || [];
            const mine = tasks.filter((t) => String(t.test_id) === String(tid));
            if (!mine.length) return null;
            mine.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
            return mine[0].status || null;
          }) : null,
        });
      },
    });
    try {
      await loadProject();
      // Bring up real-time collaboration before the first data load so the
      // initial render already reads from the shared Y.Doc when it is active.
      await tryStartCollab();
      if (hasNativeSheets()) {
        // Native multi-sheet Univer: load every worksheet's fields + rows once.
        await loadAllUniverSheets();
      } else {
        // Stale Univer bundle (no native tabs) or builtin grid: show the
        // fallback tab bar and load only the active sheet.
        buildFallbackTabs();
        await loadFields();
        await loadItems();
      }
      // Pause polling when the tab is hidden; resume (and sync at once) on return.
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) { stopPolling(); }
        else {
          startPolling();
          // Field order/config may have changed on the fields page; re-sync it
          // into the Univer table and force a re-render so column order updates.
          loadFields().then(loadItems).catch(() => silentPoll());
        }
      });
      startPolling();
      installForceSaveShortcut();
      // Defensive re-paint: some Univer builds drop cell values written before the
      // workbook finishes its first render (headers stick, data rows come up blank
      // — "shows the row count but no content"). Re-push the active sheet's rows a
      // couple of times after the engine has settled. Idempotent; needs no bundle
      // rebuild. No-op for the builtin grid, which renders synchronously.
      if (isUniver()) scheduleRepaint();
      syncRunButton();
    } catch (ex) {
      if (ex.status === 401) { window.location = LM.urls.login; return; }
      toast(ex.message, false);
    }
  }

  // Ctrl+S / ⌘S: commit the cell currently being edited (by blurring it, which
  // triggers the grid's per-cell save), wait for any in-flight PATCH to finish,
  // then reload so other users' latest changes are pulled in too.
  function installForceSaveShortcut() {
    document.addEventListener("keydown", (e) => {
      const isSave = (e.ctrlKey || e.metaKey) && !e.altKey &&
        (e.key === "s" || e.key === "S");
      if (!isSave) return;
      e.preventDefault();
      const ae = document.activeElement;
      if (ae && typeof ae.blur === "function") ae.blur(); // flush in-progress edit
      const waitIdle = (tries) => {
        if (savingCount > 0 && tries > 0) {
          setTimeout(() => waitIdle(tries - 1), 100);
          return;
        }
        loadItems()
          .then(() => toast("已强制保存并同步他人的最新修改", true))
          .catch((ex) => toast(ex.message, false));
      };
      // Give the blur-triggered save a beat to start before we wait it out.
      setTimeout(() => waitIdle(30), 120);
    });
  }

  init();
})();
