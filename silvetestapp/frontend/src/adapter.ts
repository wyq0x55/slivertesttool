/*
 * UniverGridAdapter — drives Univer Sheets as a drop-in replacement for the LAN
 * Test Matrix built-in grid (js/lanmatrix/grid.js).
 *
 * Multi-sheet: the editor exposes three logical sheets (test / const / lib) as
 * NATIVE Univer worksheet tabs inside one workbook. Each worksheet has its own
 * field set and its own rows; switching the Univer tab switches the adapter's
 * active context. The server stays the single source of authority — every edit
 * is pushed through onSave (PATCH .../items/{id}) which validates, enforces
 * permissions, keeps the optimistic lock and audits.
 *
 * Object contract expected by editor.js (value returned by LMGrid.create()):
 *   Public : setFields, setData, getSelectedIds, clearSelection, engine
 *            + multi-sheet: setSheetFields, setSheetData, patchSheetData,
 *              getActiveSheetKey, setActiveSheetKey
 *            + collaboration: setRowHighlights, setRemoteCursors, setCellErrors,
 *                             getActiveCell, isEditing
 *   Used by editor.js directly (compat): _setRowSelected, _syncSelectAll,
 *            _emitSelection
 *   Callbacks (from MountOpts): onSave, onSelectionChange, onComment, onSteps,
 *            onDelete, onInsert, onBulkDelete, onBulkDuplicate, onMove,
 *            onSheetChange(key)
 */

export interface Field {
  field_key: string;
  display_name: string;
  data_type: string;
  options?: string[];
  is_active?: boolean;
  is_required?: boolean;
  is_readonly?: boolean;
  help_text?: string;
}

export interface Item {
  id: number;
  version: number;
  row_order?: number;
  [key: string]: any;
}

export interface SheetSpec {
  key: string;   // logical sheet key persisted server-side ("test"|"const"|"lib")
  name: string;  // human tab label shown in Univer
}

export interface MountOpts {
  host: HTMLElement;
  fields?: Field[];
  // The native worksheet tabs to create, in order. When omitted a single
  // "test" sheet is used (backwards compatible with the old single-sheet mount).
  sheets?: SheetSpec[];
  onSave?: (item: { id: number; version: number }, changes: Record<string, any>) => Promise<Item>;
  onComment?: (item: { id: number }, fieldKey: string) => void;
  onSteps?: ((item: Item) => void) | null;
  onDelete?: ((item: Item) => void) | null;
  onSelectionChange?: (ids: number[]) => void;
  onInsert?: ((item: Item, where: "above" | "below") => void) | null;
  onBulkDelete?: ((ids: number[]) => void) | null;
  onBulkDuplicate?: ((ids: number[]) => void) | null;
  onMove?: ((ids: number[], dir: "up" | "down") => void) | null;
  // Fired when the user switches the active native worksheet tab.
  onSheetChange?: ((key: string) => void) | null;
}

export interface UniverDeps {
  createUniver: any;
  defaultTheme: any;
  LocaleType: any;
  mergeLocales: any;
  UniverSheetsCorePreset: any;
  UniverPresetSheetsCoreZhCN: any;
  UniverSheetsDataValidationPreset?: any;
  UniverPresetSheetsDataValidationZhCN?: any;
  UniverSheetsFilterPreset?: any;
  UniverPresetSheetsFilterZhCN?: any;
  UniverSheetsFindReplacePreset?: any;
  UniverPresetSheetsFindReplaceZhCN?: any;
  UniverSheetsSortPreset?: any;
  UniverPresetSheetsSortZhCN?: any;
  UniverSheetsConditionalFormattingPreset?: any;
  UniverPresetSheetsConditionalFormattingZhCN?: any;
  UniverSheetsTablePreset?: any;
  UniverPresetSheetsTableZhCN?: any;
  UniverSheetsCrosshairHighlightPlugin?: any;
  SheetsCrosshairHighlightZhCN?: any;
}

// Per-worksheet context: each native tab owns its field set, its rows and its
// selection. All rendering / diffing / selection logic operates on the ACTIVE
// context (this.active); other contexts are written into their own worksheet
// eagerly so switching tabs is instant.
interface SheetCtx {
  key: string;
  name: string;
  fSheet: any;
  fields: Field[];
  items: Item[];
  selected: Set<number>;
  // Signature of the visible columns at the last FULL data render, so the
  // incremental patch path (patchSheetData) can detect a column change and fall
  // back to a full re-render.
  visSig?: string;
  // Row indices currently painted with a collaborator highlight, so the next
  // setRowHighlights call knows which rows to clear (design §6.1).
  hlRows?: Set<number>;
  // "row,col" keys currently painted as server-rejected cells, so the next
  // setCellErrors call knows which cells to clear (design §12.2).
  errCells?: Set<string>;
}

const HEADER_ROWS = 1; // row 0 is the header; data starts at row 1

export class UniverGridAdapter {
  engine = "univer";

  private deps: UniverDeps;
  private opts: MountOpts;
  private host: HTMLElement;

  private specs: SheetSpec[];
  private ctxs: SheetCtx[] = [];
  private active!: SheetCtx;

  private univerAPI: any = null;
  private fWorkbook: any = null;
  private hasValidation = false;
  private hasFilter = false;

  private applying = false;      // guards programmatic writes from re-triggering onSave
  private switchingActive = false; // guards programmatic tab switches from re-firing onSheetChange
  private syncTimer: any = null; // debounce for range/paste/fill persistence
  private syncing = false;       // guards overlapping flushes
  private lastStepsKey: string | null = null; // de-dupes step-dialog open per cell
  private editing = false;       // true while the user has a cell editor open (SheetEditStarted→Ended)
  private activeCell: { id: number; col: number } | null = null; // last single-cell selection (local cursor source)

  constructor(deps: UniverDeps, opts: MountOpts) {
    this.deps = deps;
    this.opts = opts;
    this.host = opts.host;
    this.specs = (opts.sheets && opts.sheets.length)
      ? opts.sheets.slice()
      : [{ key: "test", name: "Sheet1" }];
  }

  // ------------------------------------------------------------------ mount --
  init(): void {
    const { createUniver, defaultTheme, LocaleType, mergeLocales,
            UniverSheetsCorePreset, UniverPresetSheetsCoreZhCN,
            UniverSheetsDataValidationPreset, UniverPresetSheetsDataValidationZhCN,
            UniverSheetsFilterPreset, UniverPresetSheetsFilterZhCN,
            UniverSheetsFindReplacePreset, UniverPresetSheetsFindReplaceZhCN,
            UniverSheetsSortPreset, UniverPresetSheetsSortZhCN,
            UniverSheetsConditionalFormattingPreset, UniverPresetSheetsConditionalFormattingZhCN,
            UniverSheetsTablePreset, UniverPresetSheetsTableZhCN,
            UniverSheetsCrosshairHighlightPlugin, SheetsCrosshairHighlightZhCN } = this.deps;

    if (!this.host.style.height) this.host.style.height = "70vh";
    this.host.classList.add("lm-univer-host");

    const presets: any[] = [UniverSheetsCorePreset({ container: this.host })];
    const locales: any[] = [UniverPresetSheetsCoreZhCN];
    if (typeof UniverSheetsDataValidationPreset === "function") {
      presets.push(UniverSheetsDataValidationPreset());
      if (UniverPresetSheetsDataValidationZhCN) locales.push(UniverPresetSheetsDataValidationZhCN);
      this.hasValidation = true;
    }
    if (typeof UniverSheetsFilterPreset === "function") {
      presets.push(UniverSheetsFilterPreset());
      if (UniverPresetSheetsFilterZhCN) locales.push(UniverPresetSheetsFilterZhCN);
      this.hasFilter = true;
    }
    if (typeof UniverSheetsFindReplacePreset === "function") {
      presets.push(UniverSheetsFindReplacePreset());
      if (UniverPresetSheetsFindReplaceZhCN) locales.push(UniverPresetSheetsFindReplaceZhCN);
    }
    if (typeof UniverSheetsSortPreset === "function") {
      presets.push(UniverSheetsSortPreset());
      if (UniverPresetSheetsSortZhCN) locales.push(UniverPresetSheetsSortZhCN);
    }
    if (typeof UniverSheetsConditionalFormattingPreset === "function") {
      presets.push(UniverSheetsConditionalFormattingPreset());
      if (UniverPresetSheetsConditionalFormattingZhCN) locales.push(UniverPresetSheetsConditionalFormattingZhCN);
    }
    if (typeof UniverSheetsTablePreset === "function") {
      presets.push(UniverSheetsTablePreset());
      if (UniverPresetSheetsTableZhCN) locales.push(UniverPresetSheetsTableZhCN);
    }
    if (SheetsCrosshairHighlightZhCN) locales.push(SheetsCrosshairHighlightZhCN);
    const coreLocale = mergeLocales(...locales);

    const { univer, univerAPI } = createUniver({
      locale: LocaleType.ZH_CN,
      locales: { [LocaleType.ZH_CN]: coreLocale },
      theme: defaultTheme,
      presets,
    });
    this.univerAPI = univerAPI;

    if (univer && UniverSheetsCrosshairHighlightPlugin) {
      try { univer.registerPlugin(UniverSheetsCrosshairHighlightPlugin); }
      catch (_e) { /* crosshair highlight is best-effort */ }
    }
    try {
      if (typeof univerAPI.setCrosshairHighlightEnabled === "function") {
        univerAPI.setCrosshairHighlightEnabled(true);
        if (typeof univerAPI.setCrosshairHighlightColor === "function") {
          univerAPI.setCrosshairHighlightColor("rgba(59,130,246,0.14)");
        }
      }
    } catch (_e) { /* facade not present → highlight simply stays off */ }

    this.fWorkbook = univerAPI.createWorkbook({ name: "Test Matrix" });

    // Build one native worksheet per logical sheet. The first reuses the
    // workbook's default sheet (renamed); the rest are created after it.
    this.ctxs = this.specs.map((spec, i) => {
      const fSheet = this._ensureSheet(spec.name, i === 0);
      const fields = i === 0 ? (this.opts.fields || []).slice() : [];
      return { key: spec.key, name: spec.name, fSheet,
               fields, items: [], selected: new Set<number>() };
    });
    this.active = this.ctxs[0];
    // Make sure the first tab is the active one.
    this._activateFSheet(this.active);

    try {
      const ev = univerAPI.Event;
      if (ev && ev.LifeCycleChanged && typeof univerAPI.addEvent === "function") {
        univerAPI.addEvent(ev.LifeCycleChanged, (p: any) => {
          const stage = p && p.stage;
          const rendered = univerAPI.Enum && univerAPI.Enum.LifecycleStages
            ? univerAPI.Enum.LifecycleStages.Rendered : undefined;
          if (rendered === undefined || stage === rendered) {
            this._applyFreeze(this.active);
          }
        });
      }
    } catch (_e) { /* header/freeze are best-effort */ }

    this._bindEvents();
    // Initial paint for every worksheet (headers now; data arrives via setSheetData).
    this.ctxs.forEach((ctx) => {
      this._renderHeader(ctx);
      this._renderRows(ctx);
    });
    this._applyFreeze(this.active);
  }

  // Reuse the default sheet for the first requested one, then create more.
  private _ensureSheet(name: string, isFirst: boolean): any {
    try {
      const existing = this.fWorkbook.getSheetByName(name);
      if (existing) return existing;
    } catch (_e) { /* ignore */ }
    if (isFirst) {
      try {
        const active = this.fWorkbook.getActiveSheet();
        if (active && typeof active.setName === "function") {
          active.setName(name);
          // The default worksheet is only ~20 columns wide — too narrow for the
          // 23-column Test-Matrix. Widen it now so the header/data paint.
          this._ensureWidth(active, 64);
          return active;
        }
      } catch (_e) { /* ignore */ }
    }
    try { const s = this.fWorkbook.create(name, 1000, 64); if (s) return s; } catch (_e) { /* try next */ }
    try { const s = this.fWorkbook.insertSheet(name); if (s) return s; } catch (_e) { /* give up */ }
    return this.fWorkbook.getActiveSheet();
  }

  private _activateFSheet(ctx: SheetCtx): void {
    if (!ctx || !ctx.fSheet) return;
    try {
      if (typeof this.fWorkbook.setActiveSheet === "function") {
        this.fWorkbook.setActiveSheet(ctx.fSheet);
      } else if (typeof ctx.fSheet.activate === "function") {
        ctx.fSheet.activate();
      }
    } catch (_e) { /* best-effort */ }
  }

  // --------------------------------------------------------------- public API -
  private _ctx(key: string): SheetCtx | null {
    return this.ctxs.find((c) => c.key === key) || null;
  }

  setSheetFields(key: string, fields: Field[]): void {
    const ctx = this._ctx(key);
    if (!ctx) return;
    ctx.fields = (fields || []).slice();
    this._renderHeader(ctx);
    this._applyValidations(ctx);
    if (ctx === this.active) this._applyFreeze(ctx);
  }

  setSheetData(key: string, items: Item[]): void {
    const ctx = this._ctx(key);
    if (!ctx) return;
    ctx.items = items || [];
    const present = new Set(ctx.items.map((i) => i.id));
    ctx.selected.forEach((id) => { if (!present.has(id)) ctx.selected.delete(id); });
    this._renderRows(ctx);
    if (ctx === this.active) { this._applyFreeze(ctx); this._emitSelection(); }
  }

  // Paint per-row collaborator highlights on the active worksheet (Phase 2,
  // design §6.1). ``map`` is ``{ id: colorHex }``: rows whose item id is present
  // get a faint tint of that collaborator's colour, all previously-tinted rows
  // are cleared. Univer is canvas-rendered, so unlike the fallback grid there is
  // no DOM overlay — the tint is written through the facade. Runs under
  // ``applying`` so these style writes never loop back as saves.
  setRowHighlights(map: Record<number, string>): void {
    const ctx = this.active;
    if (!ctx || !ctx.fSheet) return;
    const vis = this._visibleFields(ctx);
    if (!vis.length) return;
    const m = map || {};
    const prev = ctx.hlRows || new Set<number>();
    const next = new Set<number>();
    this.applying = true;
    try {
      ctx.items.forEach((it, idx) => {
        const color = m[it.id as number];
        if (!color) return;
        next.add(idx);
        try {
          ctx.fSheet.getRange(HEADER_ROWS + idx, 0, 1, vis.length)
            .setBackgroundColor(this._tint(color));
        } catch (_e) { /* best-effort per row */ }
      });
      prev.forEach((idx) => {
        if (next.has(idx) || idx >= ctx.items.length) return;
        try {
          ctx.fSheet.getRange(HEADER_ROWS + idx, 0, 1, vis.length)
            .setBackgroundColor(null);
        } catch (_e) { /* best-effort per row */ }
      });
    } finally { this.applying = false; }
    ctx.hlRows = next;
  }

  // True only while a real cell-editor session is open (a peer's remote apply
  // would clobber the user's in-progress keystrokes). Unlike a DOM activeElement
  // probe this does NOT fire for a merely-selected cell, so real-time sync keeps
  // flowing the instant the user stops editing (design §1.3).
  isEditing(): boolean { return this.editing; }

  // The active cell as ``{ id, col }`` (col = visible-column index) or null.
  // The editor publishes it into awareness as this user's cursor so peers can
  // draw a precise remote overlay (design §6.1).
  getActiveCell(): { id: number; col: number } | null { return this.activeCell; }

  // Precise remote-cursor overlay for the Univer engine. Univer is canvas-
  // rendered and the community facade exposes no stable cell-pixel-rect API
  // across versions, so an accurate overlay cannot be drawn here — return false
  // so the editor falls back to the row-highlight presence (setRowHighlights),
  // which IS reliable through the facade. Peers running the fallback grid still
  // see this user's precise cursor because getActiveCell publishes the column.
  setRemoteCursors(_cursors: any[]): boolean { return false; }

  // Mark cells the server rejected during materialization (design §12.2). ``map``
  // is ``{ rowId: { cells: [field_key...], message } }``; an empty ``cells`` list
  // flags the whole visible row. Reachable through the facade (setBackgroundColor
  // + a cell note carrying the message), so Univer users get the red cell too.
  // Re-applied after every render; clears cells no longer in error.
  setCellErrors(map: Record<number, { cells: string[]; message: string }>): void {
    const ctx = this.active;
    if (!ctx || !ctx.fSheet) return;
    const vis = this._visibleFields(ctx);
    if (!vis.length) return;
    const colOf: Record<string, number> = {};
    vis.forEach((f, i) => { colOf[f.field_key] = i; });
    const m = map || {};
    const prev = ctx.errCells || new Set<string>();
    const next = new Set<string>();
    const rowByUuidId: Record<number, number> = {};
    ctx.items.forEach((it, idx) => { rowByUuidId[it.id as number] = idx; });
    this.applying = true;
    try {
      Object.keys(m).forEach((idStr) => {
        const idx = rowByUuidId[Number(idStr)];
        if (idx === undefined) return;
        const err = m[Number(idStr)] || { cells: [], message: "" };
        const cols = (err.cells && err.cells.length)
          ? err.cells.map((k) => colOf[k]).filter((c) => c !== undefined)
          : vis.map((_f, i) => i);
        cols.forEach((c) => {
          next.add(idx + "," + c);
          try {
            const rng = ctx.fSheet.getRange(HEADER_ROWS + idx, c, 1, 1);
            rng.setBackgroundColor("#fff4f4");
            if (typeof rng.setNote === "function" && err.message) {
              try { rng.setNote(err.message); } catch (_e) { /* note optional */ }
            }
          } catch (_e) { /* best-effort per cell */ }
        });
      });
      prev.forEach((key) => {
        if (next.has(key)) return;
        const [r, c] = key.split(",").map(Number);
        if (r >= ctx.items.length) return;
        try {
          const rng = ctx.fSheet.getRange(HEADER_ROWS + r, c, 1, 1);
          rng.setBackgroundColor(null);
          if (typeof rng.setNote === "function") {
            try { rng.setNote(""); } catch (_e) { /* note optional */ }
          }
        } catch (_e) { /* best-effort per cell */ }
      });
    } finally { this.applying = false; }
    ctx.errCells = next;
  }

  // Mix a collaborator colour with white to a faint tint suitable as a row
  // background (the full colour would drown the cell text).
  private _tint(hex: string): string {
    try {
      let h = String(hex).replace("#", "");
      if (h.length === 3) h = h.split("").map((c) => c + c).join("");
      const n = parseInt(h, 16);
      const mix = (c: number) => Math.round(c + (255 - c) * 0.85);
      const to2 = (c: number) => mix(c).toString(16).padStart(2, "0");
      return "#" + to2((n >> 16) & 255) + to2((n >> 8) & 255) + to2(n & 255);
    } catch (_e) { return "#eef1f8"; }
  }

  // Incremental remote apply (real-time collaboration, design §1.3). When the
  // incoming rows have the SAME id sequence AND the SAME visible columns as what
  // is already rendered, write ONLY the cells whose value changed — leaving
  // scroll position, the current selection and any active edit in OTHER cells
  // untouched. Any structural difference (rows added/removed/reordered, a new
  // row's temporary id flipping to its server id, or a column change) falls back
  // to a full `setSheetData` re-render, so correctness never depends on the diff.
  //
  // This is only ever driven by remote Y.Doc changes; local edits still flow out
  // through the normal edit→onSave path. Writes run under `applying = true`, so
  // `_scheduleSync` ignores them and they cannot loop back as a save.
  patchSheetData(key: string, items: Item[]): void {
    const ctx = this._ctx(key);
    if (!ctx) return;
    const next = items || [];
    const prev = ctx.items;
    const vis = this._visibleFields(ctx);
    const visSig = vis.map((f) => f.field_key).join("\u0001");

    let structural = next.length !== prev.length || visSig !== ctx.visSig;
    if (!structural) {
      for (let i = 0; i < next.length; i++) {
        if (next[i].id !== prev[i].id) { structural = true; break; }
      }
    }
    if (structural) { this.setSheetData(key, items); return; }

    this.applying = true;
    try {
      for (let r = 0; r < next.length; r++) {
        const a = prev[r];
        const b = next[r];
        for (let c = 0; c < vis.length; c++) {
          const f = vis[c];
          const nv = this._normalize(this._displayValue(b[f.field_key], f), f);
          const ov = this._normalize(this._displayValue(a[f.field_key], f), f);
          if (String(nv) === String(ov)) continue;
          try {
            ctx.fSheet.getRange(HEADER_ROWS + r, c, 1, 1)
              .setValue(this._displayValue(b[f.field_key], f));
          } catch (_e) { /* a single-cell failure must not abort the batch */ }
        }
      }
      ctx.items = next;
    } finally {
      this.applying = false;
    }
    // Row-id sequence is unchanged, so the current selection ids stay valid and
    // there is no need to re-emit selection or re-freeze.
  }

  getActiveSheetKey(): string { return this.active ? this.active.key : ""; }

  setActiveSheetKey(key: string): void {
    const ctx = this._ctx(key);
    if (!ctx || ctx === this.active) return;
    this.switchingActive = true;
    try {
      this._activateFSheet(ctx);
      this.active = ctx;
      this._applyFreeze(ctx);
      this._emitSelection();
    } finally {
      this.switchingActive = false;
    }
  }

  // Backwards-compatible single-sheet API → operate on the active worksheet.
  setFields(fields: Field[]): void {
    if (!this.active) return;
    this.setSheetFields(this.active.key, fields);
  }

  setData(items: Item[]): void {
    if (!this.active) return;
    this.setSheetData(this.active.key, items);
  }

  getSelectedIds(): number[] {
    const ctx = this.active;
    if (!ctx) return [];
    return ctx.items.map((i) => i.id).filter((id) => ctx.selected.has(id));
  }

  clearSelection(): void {
    if (this.active) this.active.selected.clear();
    this._emitSelection();
  }

  // editor.js reaches into these three after row moves — keep them working.
  _setRowSelected(id: number, on: boolean): void {
    if (!this.active) return;
    if (on) this.active.selected.add(id); else this.active.selected.delete(id);
  }

  _syncSelectAll(): void { /* no dedicated "select-all" checkbox in Univer */ }

  _emitSelection(): void {
    try { (this.opts.onSelectionChange || (() => {}))(this.getSelectedIds()); }
    catch (_e) { /* noop */ }
  }

  // ---------------------------------------------------------------- internals -
  private _visibleFields(ctx: SheetCtx): Field[] {
    return ctx.fields.filter((f) => f.is_active !== false);
  }

  // The "steps" field stores a JSON手順 document. A spreadsheet cell must never
  // show (or let anyone hand-edit) that raw JSON, so we treat it as a read-only
  // label cell showing the step count. Selecting it opens the bottom step-detail
  // drawer (see _maybeOpenSteps).
  private _isStepsField(f: Field): boolean {
    return !!f && (f.field_key === "steps" || f.data_type === "steps");
  }

  private _stepsLabel(raw: any): string {
    let n = 0;
    try {
      const doc = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {});
      if (doc && Array.isArray(doc.steps)) n = doc.steps.length;
    } catch (_e) { n = 0; }
    return n > 0 ? `\u270E 步骤明细 (${n})` : "\u270E 步骤明细";
  }

  private _colToField(ctx: SheetCtx, col: number): Field | null {
    const vis = this._visibleFields(ctx);
    return col >= 0 && col < vis.length ? vis[col] : null;
  }

  private _rowToItem(ctx: SheetCtx, row: number): Item | null {
    const idx = row - HEADER_ROWS;
    return idx >= 0 && idx < ctx.items.length ? ctx.items[idx] : null;
  }

  // Univer's default worksheet is only ~20 columns wide, but the Test-Matrix
  // field set has 23+ columns. When the header/data range exceeds the sheet's
  // width, getRange(...).setValues() throws and the whole sheet paints blank —
  // the exact "test sheet shows no header and no content while const/lib (with
  // far fewer columns) render fine" symptom. Grow the grid to fit before any
  // range write. Best-effort across the facade's differing width APIs.
  private _ensureWidth(sheet: any, need: number): void {
    if (!sheet || need <= 0) return;
    try {
      let have = 0;
      if (typeof sheet.getMaxColumns === "function") have = sheet.getMaxColumns();
      else if (typeof sheet.getColumnCount === "function") have = sheet.getColumnCount();
      if (have >= need) return;
      if (typeof sheet.setColumnCount === "function") { sheet.setColumnCount(need); return; }
      if (typeof sheet.insertColumns === "function") {
        sheet.insertColumns(Math.max(have - 1, 0), need - have);
      }
    } catch (_e) { /* width grow is best-effort; render still tries */ }
  }

  private _renderHeader(ctx: SheetCtx): void {
    if (!ctx.fSheet) return;
    const vis = this._visibleFields(ctx);
    this._ensureWidth(ctx.fSheet, vis.length + 1);
    this.applying = true;
    try {
      const labels = vis.map((f) =>
        (f.display_name || f.field_key) + (f.is_required ? " *" : "") + (f.is_readonly ? " 🔒" : ""));
      if (labels.length) {
        const range = ctx.fSheet.getRange(0, 0, 1, labels.length);
        range.setValues([labels]);
        try { range.setFontWeight("bold"); } catch (_e) { /* optional */ }
      }
    } catch (e) {
      console.warn("[LMUniver] header render failed:", e);
    } finally {
      this.applying = false;
    }
  }

  private _renderRows(ctx: SheetCtx): void {
    if (!ctx.fSheet) return;
    const vis = this._visibleFields(ctx);
    this._ensureWidth(ctx.fSheet, vis.length + 1);
    this.applying = true;
    try {
      const clearRows = Math.max(ctx.items.length + 50, 50);
      try {
        const clearRange = ctx.fSheet.getRange(HEADER_ROWS, 0, clearRows, Math.max(vis.length, 1));
        clearRange.clearContent();
        // clearContent keeps cell formatting, so any collaborator row tint from a
        // previous render would linger on now-stale rows. Wipe the data-region
        // background too; live cursors are re-applied afterwards by the editor.
        try { clearRange.setBackgroundColor && clearRange.setBackgroundColor(null); } catch (_e) { /* optional */ }
        ctx.hlRows = new Set();
      } catch (_e) { /* older facade: skip clear */ }

      if (ctx.items.length && vis.length) {
        const matrix = ctx.items.map((it) =>
          vis.map((f) => this._displayValue(it[f.field_key], f)));
        ctx.fSheet.getRange(HEADER_ROWS, 0, matrix.length, vis.length).setValues(matrix);
      }
      vis.forEach((f, col) => {
        if (!this._isStepsField(f)) return;
        try { ctx.fSheet.setColumnWidth && ctx.fSheet.setColumnWidth(col, 130); } catch (_e) { /* best-effort */ }
        try {
          ctx.fSheet.getRange(HEADER_ROWS, col, Math.max(ctx.items.length, 1), 1)
            .setHorizontalAlignment("center");
        } catch (_e) { /* best-effort */ }
      });
      this._applyValidations(ctx);
      this._applyFilter(ctx, vis.length);
      // Record the columns this full render was drawn with, so a later
      // incremental patch can detect a column change and re-render fully.
      ctx.visSig = vis.map((f) => f.field_key).join("\u0001");
    } catch (e) {
      console.warn("[LMUniver] rows render failed:", e);
    } finally {
      this.applying = false;
    }
  }

  private _applyFilter(ctx: SheetCtx, colCount: number): void {
    if (!ctx.fSheet || !this.hasFilter || colCount <= 0) return;
    const rows = HEADER_ROWS + Math.max(ctx.items.length, 1);
    try {
      const existing = ctx.fSheet.getFilter && ctx.fSheet.getFilter();
      if (existing && typeof existing.remove === "function") {
        try { existing.remove(); } catch (_e) { /* ignore */ }
      }
      const range = ctx.fSheet.getRange(0, 0, rows, colCount);
      if (range && typeof range.createFilter === "function") range.createFilter();
    } catch (_e) { /* filter is best-effort */ }
  }

  private _applyFreeze(ctx: SheetCtx): void {
    if (!ctx.fSheet || HEADER_ROWS <= 0) return;
    try {
      if (typeof ctx.fSheet.setFreeze === "function") {
        ctx.fSheet.setFreeze({ xSplit: 0, ySplit: HEADER_ROWS, startRow: HEADER_ROWS, startColumn: 0 });
      } else if (typeof ctx.fSheet.setFrozenRows === "function") {
        ctx.fSheet.setFrozenRows(HEADER_ROWS);
      }
    } catch (_e) { /* freeze is best-effort */ }
    const vis = this._visibleFields(ctx);
    if (!vis.length) return;
    this.applying = true;
    try {
      const hdr = ctx.fSheet.getRange(0, 0, HEADER_ROWS, vis.length);
      try { hdr.setBackgroundColor && hdr.setBackgroundColor("#eef1f8"); } catch (_e) { /* optional */ }
      try { hdr.setFontWeight && hdr.setFontWeight("bold"); } catch (_e) { /* optional */ }
    } catch (_e) { /* styling is best-effort */ }
    finally { this.applying = false; }
  }

  private _displayValue(raw: any, f: Field): any {
    if (f && this._isStepsField(f)) return this._stepsLabel(raw);
    if (raw == null) return "";
    if (Array.isArray(raw)) return raw.join("; ");
    if (raw === true) return "是";
    if (raw === false) return "否";
    return raw;
  }

  private _applyValidations(ctx: SheetCtx): void {
    if (!ctx.fSheet || !this.univerAPI || !this.hasValidation) return;
    if (typeof this.univerAPI.newDataValidation !== "function") return;
    const vis = this._visibleFields(ctx);
    const rows = Math.max(ctx.items.length, 1);
    vis.forEach((f, col) => {
      if (this._isStepsField(f) || f.is_readonly) return;
      let rule: any = null;
      try {
        const b = this.univerAPI.newDataValidation();
        if (f.data_type === "boolean") {
          rule = b.requireValueInList(["是", "否"], false, true).build();
        } else if (f.data_type === "single_select" && (f.options || []).length) {
          rule = b.requireValueInList((f.options as string[]).slice(), false, true).build();
        } else if (f.data_type === "multi_select" && (f.options || []).length) {
          rule = b.requireValueInList((f.options as string[]).slice(), true, true).build();
        } else if (f.data_type === "date" || f.data_type === "datetime") {
          rule = b.requireDateBetween(new Date(1900, 0, 1), new Date(2999, 11, 31))
            .setOptions({ allowBlank: true, showErrorMessage: false }).build();
        }
      } catch (_e) { rule = null; }
      if (!rule) {
        try { ctx.fSheet.getRange(HEADER_ROWS, col, rows, 1).setDataValidation(null); }
        catch (_e2) { /* ignore */ }
        return;
      }
      try {
        ctx.fSheet.getRange(HEADER_ROWS, col, rows, 1).setDataValidation(rule);
      } catch (_e) { /* validation is best-effort */ }
    });
  }

  // ------------------------------------------------------------------ events --
  private _bindEvents(): void {
    const api = this.univerAPI;
    if (!api || !api.Event) {
      console.warn("[LMUniver] facade Event API unavailable; edits will not persist");
      return;
    }
    const evs = api.Event || {};
    const changeEvents = [
      "SheetEditEnded", "SheetValueChanged", "CellValueChanged",
      "ClipboardPasted", "SheetPasted", "SheetRangePasted", "Paste",
      "SheetFillChanged", "RangeFilled",
    ];
    let bound = 0;
    changeEvents.forEach((name) => {
      if (!evs[name]) return;
      try { api.addEvent(evs[name], () => this._scheduleSync()); bound++; }
      catch (_e) { /* skip */ }
    });
    if (typeof api.onCommandExecuted === "function") {
      try {
        api.onCommandExecuted((cmd: any) => {
          const id = cmd && cmd.id ? String(cmd.id) : "";
          if (/paste|set-?range-?values|set-?range|fill|clear-?selection-?content|delete-?range/i.test(id)) {
            this._scheduleSync();
          } else if (/set-?worksheet-?active|active-?sheet|activate-?sheet/i.test(id)) {
            this._onActiveSheetMaybeChanged();
          }
        });
        bound++;
      } catch (_e) { /* optional */ }
    }
    if (!bound) console.warn("[LMUniver] no change event could be bound; edits may not persist");

    // Track the cell-editor open/close so the editor can tell "actively typing"
    // apart from "merely focused" (design §1.3 real-time apply). Univer keeps a
    // hidden input focused to capture keystrokes even when the user is only
    // selecting, so a DOM activeElement check misfires and freezes remote sync;
    // these events flip only on a real edit session.
    try {
      if (evs.SheetEditStarted) api.addEvent(evs.SheetEditStarted, () => { this.editing = true; });
      if (evs.SheetEditEnded) api.addEvent(evs.SheetEditEnded, () => { this.editing = false; });
    } catch (_e) { /* edit-state tracking is optional */ }

    // Native worksheet-tab switches: probe the various event names Univer builds
    // expose, and reconcile the active context on each.
    const activeEvents = ["SheetActivated", "ActiveSheetChanged", "SheetActiveChanged",
                          "WorksheetActivated", "SheetActivate"];
    activeEvents.forEach((name) => {
      if (!evs[name]) return;
      try { api.addEvent(evs[name], () => this._onActiveSheetMaybeChanged()); }
      catch (_e) { /* skip */ }
    });

    try {
      if (evs.SelectionChanged) api.addEvent(evs.SelectionChanged, (p: any) => this._onSelectionChanged(p));
    } catch (_e) { /* selection tracking is optional */ }
  }

  // Reconcile this.active with Univer's currently-active worksheet. Fired both by
  // native tab-click events and by the active-sheet command. Skips programmatic
  // switches (switchingActive) so setActiveSheetKey doesn't re-enter.
  private _onActiveSheetMaybeChanged(): void {
    if (this.switchingActive) return;
    let name: string | null = null;
    try {
      const cur = this.fWorkbook.getActiveSheet();
      if (cur) {
        if (typeof cur.getSheetName === "function") name = cur.getSheetName();
        else if (typeof cur.getName === "function") name = cur.getName();
      }
    } catch (_e) { /* ignore */ }
    if (!name) return;
    const ctx = this.ctxs.find((c) => c.name === name);
    if (!ctx || ctx === this.active) return;
    this.active = ctx;
    this.lastStepsKey = null;
    this._applyFreeze(ctx);
    this._emitSelection();
    try { (this.opts.onSheetChange || (() => {}))(ctx.key); }
    catch (e) { console.warn("[LMUniver] onSheetChange failed:", e); }
  }

  private _scheduleSync(): void {
    if (this.applying) return;
    if (this.syncTimer) clearTimeout(this.syncTimer);
    this.syncTimer = setTimeout(() => { this.syncTimer = null; this._flushSync(); }, 90);
  }

  // Diff the active worksheet's data region against its local cache and persist
  // every change, grouped per item (one PATCH per row). Handles single edits,
  // Delete-key clears, drag-fill and multi-cell paste uniformly.
  private async _flushSync(): Promise<void> {
    if (this.applying) return;
    if (this.syncing) { this._scheduleSync(); return; }
    const ctx = this.active;
    if (!ctx) return;
    const vis = this._visibleFields(ctx);
    if (!ctx.fSheet || !vis.length || !ctx.items.length) return;
    if (!this.opts.onSave) return;

    let values: any[][];
    try {
      values = ctx.fSheet.getRange(HEADER_ROWS, 0, ctx.items.length, vis.length).getValues() || [];
    } catch (_e) { return; }

    type Group = { item: Item; changes: Record<string, any>; cells: { row: number; col: number; field: Field }[] };
    const groups = new Map<number, Group>();
    const reverts: { row: number; col: number; display: any }[] = [];

    for (let r = 0; r < ctx.items.length; r++) {
      const item = ctx.items[r];
      const rowVals = values[r] || [];
      for (let c = 0; c < vis.length; c++) {
        const field = vis[c];
        const curDisplay = this._displayValue(item[field.field_key], field);
        const newNorm = this._normalize(rowVals[c], field);
        const curNorm = this._normalize(curDisplay, field);
        if (String(newNorm) === String(curNorm)) continue;
        if (field.is_readonly || this._isStepsField(field)) {
          reverts.push({ row: HEADER_ROWS + r, col: c, display: curDisplay });
          continue;
        }
        let g = groups.get(item.id);
        if (!g) { g = { item, changes: {}, cells: [] }; groups.set(item.id, g); }
        g.changes[field.field_key] = newNorm;
        g.cells.push({ row: HEADER_ROWS + r, col: c, field });
      }
    }

    reverts.forEach((rv) => this._revertCell(ctx, rv.row, rv.col, rv.display));
    if (!groups.size) return;

    this.syncing = true;
    let saved = 0, conflicts = 0, failures = 0;
    try {
      for (const g of groups.values()) {
        try {
          const updated = await this.opts.onSave(
            { id: g.item.id, version: g.item.version }, g.changes);
          Object.assign(g.item, updated);
          saved++;
          g.cells.forEach((cell) =>
            this._revertCell(ctx, cell.row, cell.col,
              this._displayValue(g.item[cell.field.field_key], cell.field)));
        } catch (ex: any) {
          if (ex && ex.code === "VERSION_CONFLICT" && ex.details && ex.details.server_data) {
            g.item.version = ex.details.server_version;
            Object.assign(g.item, ex.details.server_data);
            conflicts++;
          } else {
            failures++;
          }
          g.cells.forEach((cell) =>
            this._revertCell(ctx, cell.row, cell.col,
              this._displayValue(g.item[cell.field.field_key], cell.field)));
        }
      }
    } finally {
      this.syncing = false;
    }
    this._reportSync(groups.size, saved, conflicts, failures);
  }

  private _reportSync(total: number, saved: number, conflicts: number, failures: number): void {
    const toast = (window as any).LMToast;
    if (!toast) return;
    if (total === 1) {
      if (conflicts) toast("该行已被他人修改，已刷新为最新值", false);
      else if (failures) toast("保存失败", false);
      return;
    }
    if (!conflicts && !failures) { toast(`已批量保存 ${saved} 行`, true); return; }
    const parts = [`已保存 ${saved} 行`];
    if (conflicts) parts.push(`${conflicts} 行被他人修改已刷新`);
    if (failures) parts.push(`${failures} 行失败`);
    toast(parts.join("，"), false);
  }

  private _normalize(value: any, field: Field): any {
    if (value == null) return "";
    if (field.data_type === "boolean") {
      const s = String(value).trim();
      return s === "是" ? "是" : s === "否" ? "否" : "";
    }
    if (Array.isArray(value)) return value.join(";");
    return typeof value === "string" ? value : String(value);
  }

  private _revertCell(ctx: SheetCtx, row: number, col: number, display: any): void {
    if (!ctx.fSheet) return;
    this.applying = true;
    try { ctx.fSheet.getRange(row, col, 1, 1).setValue(display); }
    catch (_e) { /* ignore */ }
    finally { this.applying = false; }
  }

  private _onSelectionChanged(p: any): void {
    const ctx = this.active;
    if (!ctx) return;
    const ranges: any[] = (p && (p.selections || p.ranges)) ||
                          (p && p.range ? [p.range] : []);
    ctx.selected.clear();
    let single: { row: number; col: number } | null = null;
    ranges.forEach((r) => {
      const s = r.startRow ?? r.row;
      const e = r.endRow ?? s;
      const sc = r.startColumn ?? r.column ?? r.col;
      const ec = r.endColumn ?? sc;
      if (s == null) return;
      for (let row = Math.max(s, HEADER_ROWS); row <= e; row++) {
        const it = this._rowToItem(ctx, row);
        if (it) ctx.selected.add(it.id);
      }
      if (s === e && sc != null && sc === ec) single = { row: Number(s), col: Number(sc) };
    });
    // Record the active cell so the editor can publish it as the local cursor
    // (design §6.1); col is the visible-column index, matching FallbackGrid.
    if (single) {
      const it = this._rowToItem(ctx, (single as { row: number; col: number }).row);
      this.activeCell = it ? { id: it.id as number, col: (single as { row: number; col: number }).col } : null;
    } else {
      this.activeCell = null;
    }
    this._emitSelection();
    this._maybeOpenSteps(single);
  }

  private _maybeOpenSteps(single: { row: number; col: number } | null): void {
    const ctx = this.active;
    if (!ctx || !single || !this.opts.onSteps) { this.lastStepsKey = null; return; }
    const field = this._colToField(ctx, single.col);
    if (!field || !this._isStepsField(field)) { this.lastStepsKey = null; return; }
    const item = this._rowToItem(ctx, single.row);
    if (!item) { this.lastStepsKey = null; return; }
    const key = `${ctx.key}:${item.id}:${single.col}`;
    if (key === this.lastStepsKey) return;
    this.lastStepsKey = key;
    try { this.opts.onSteps(item); } catch (e) { console.warn("[LMUniver] onSteps failed:", e); }
  }
}
