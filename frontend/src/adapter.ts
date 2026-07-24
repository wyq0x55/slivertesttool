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
 *            + collaboration: setRemoteSelections, setCellErrors,
 *                             getActiveSelection, isEditing
 *   Used by editor.js directly (compat): _setRowSelected, _syncSelectAll,
 *            _emitSelection
 *   Callbacks (from MountOpts): onSave, onSelectionChange, onComment, onSteps,
 *            onDelete, onInsert, onBulkDelete, onBulkDuplicate, onMove,
 *            onSheetChange(key)
 */

import { CollabOverlay } from "./collab_overlay";
import type { OverlaySelection } from "./collab_overlay";
import { CellTooltip } from "./cell_tooltip";

// ---------------------------------------------------------------------------
// Multi-line cell support.
//
// A plain-string cell value that contains a newline is INVALID for Univer: its
// getCellDocumentModel builds a single-paragraph document whose dataStream still
// holds the raw "\n", so the internal break is never registered as a paragraph.
// Rendering tolerates it (shows the first line) but the cell EDITOR chokes on the
// orphan character and opens blank — the "double-click shows nothing" bug. Univer
// requires multi-line text to be RICH TEXT (cell.p / IDocumentData) where every
// line break is a "\r" (DataStreamTreeTokenType.PARAGRAPH) with a matching entry
// in `paragraphs`, terminated by "\r\n" ("\n" = SECTION_BREAK).
//
// We therefore store any value containing "\n" as rich text (plain text only — no
// multi-segment styling, per requirements) and keep single-line values as plain
// strings (fast path, unchanged). Row height stays uniform: rich text does NOT
// enable wrap, so the cell still renders at one line height (the full text is
// revealed by the hover bubble).
// ---------------------------------------------------------------------------

/** True when a value must be stored as rich text (string with an embedded LF). */
export function isMultiline(v: any): boolean {
  return typeof v === "string" && v.indexOf("\n") !== -1;
}

/**
 * Build a minimal, VALID Univer IDocumentData for a multi-line plain string.
 * Lines are joined with "\r" (paragraph breaks) and the stream ends with "\r\n";
 * `paragraphs` carries one entry per "\r" so the document tree — and the cell
 * editor — parse correctly.
 */
export function buildRichTextDoc(text: string): any {
  const lines = String(text).split("\n");
  const dataStream = lines.join("\r") + "\r\n";
  const paragraphs: Array<{ startIndex: number }> = [];
  let idx = 0;
  for (let i = 0; i < lines.length; i++) {
    idx += lines[i].length;        // index of the "\r" that terminates this line
    paragraphs.push({ startIndex: idx });
    idx += 1;                      // step past the "\r"
  }
  return {
    id: "d",
    body: {
      dataStream,
      textRuns: [{ st: 0, ed: Math.max(0, dataStream.length - 1), ts: {} }],
      paragraphs,
      sectionBreaks: [{ startIndex: dataStream.length - 1 }],
    },
    documentStyle: {},
  };
}

/** Normalize any CR/CRLF newline convention to a bare LF. */
export function normalizeNewlines(s: string): string {
  return s.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

/**
 * Wrap a display value for writing to Univer: a multi-line string becomes rich
 * text (ICellData.p) so the cell editor can load it; everything else stays a
 * plain primitive. Shared by the main grid and the step-detail view.
 */
export function toCellData(display: any): any {
  return isMultiline(display) ? { p: buildRichTextDoc(display) } : display;
}

/**
 * Flatten a cell read from `getValueAndRichTextValues()` to plain text: a
 * RichTextValue (multi-line cell) exposes `toPlainText()`; everything else is a
 * primitive CellValue. Univer's rich text always carries a trailing paragraph
 * break, so a single trailing newline is stripped to match the stored value.
 */
export function cellReadToText(cell: any): any {
  if (cell != null && typeof cell.toPlainText === "function") {
    let s = normalizeNewlines(String(cell.toPlainText()));
    s = s.replace(/\n$/, "");
    return s;
  }
  return cell;
}

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
  // Univer DI identifiers needed by the collaborative border overlay to project
  // sheet (row,col) -> canvas pixels. An IIFE/UMD bundle does NOT expose these on
  // any global, so they MUST be injected here (main.ts imports them from
  // @univerjs/engine-render and @univerjs/sheets-ui). When omitted the overlay
  // degrades to a no-op and no remote cursor is drawn.
  IRenderManagerService?: any;
  SheetSkeletonManagerService?: any;
  // Hover service for the truncated-cell full-text bubble (see cell_tooltip.ts).
  HoverManagerService?: any;
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
  private univer: any = null;          // raw Univer instance (for __getInjector)
  private overlay: CollabOverlay | null = null; // remote-selection border overlay
  private cellTooltip: CellTooltip | null = null; // truncated-cell full-text bubble
  private fWorkbook: any = null;
  private hasValidation = false;
  private hasFilter = false;

  private applying = false;      // guards programmatic writes from re-triggering onSave
  private switchingActive = false; // guards programmatic tab switches from re-firing onSheetChange
  private syncTimer: any = null; // debounce for range/paste/fill persistence
  private syncing = false;       // guards overlapping flushes
  // True from the moment a native Univer row insert/remove is intercepted until
  // the host reloads (setSheetData). While set, _flushSync bails out so the
  // transient, shifted-but-stale grid is never mistaken for cell edits and
  // half-saved. Cleared by setSheetData or, as a safety net, a timeout.
  private structuralPending = false;
  private structuralTimer: any = null;
  private lastStepsKey: string | null = null; // de-dupes step-dialog open per cell
  private editing = false;       // true while the user has a cell editor open (SheetEditStarted→Ended)
  private activeCell: { id: number; col: number } | null = null; // last single-cell selection (anchor source)
  // Full local selection published into awareness so peers can draw a border
  // overlay (design §6.1): anchor cell + selected row ids + visible column span.
  private activeSelection: {
    anchor: { id: number; col: number } | null;
    rowIds: number[];
    cols: [number, number] | null;
  } | null = null;

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
    this.univer = univer;
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
            this._ensureOverlay();
            this._ensureCellTooltip();
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
    // A reload is the authoritative end of any native row insert/remove we
    // intercepted, so lift the sync-suppression guard here.
    this._endStructural();
    const ctx = this._ctx(key);
    if (!ctx) return;
    ctx.items = items || [];
    const present = new Set(ctx.items.map((i) => i.id));
    ctx.selected.forEach((id) => { if (!present.has(id)) ctx.selected.delete(id); });
    this._renderRows(ctx);
    if (ctx === this.active) { this._applyFreeze(ctx); this._emitSelection(); }
  }

  // True only while a real cell-editor session is open (a peer's remote apply
  // would clobber the user's in-progress keystrokes). Unlike a DOM activeElement
  // probe this does NOT fire for a merely-selected cell, so real-time sync keeps
  // flowing the instant the user stops editing (design §1.3).
  isEditing(): boolean { return this.editing; }

  // The full local selection (design §6.1): the anchor (active) cell plus the
  // set of selected row ids and the visible column span. The editor maps the ids
  // to row uuids and publishes it into awareness so peers can draw a border
  // overlay that is correct regardless of each peer's own sort/filter view.
  getActiveSelection(): {
    anchor: { id: number; col: number } | null;
    rowIds: number[];
    cols: [number, number] | null;
  } | null {
    return this.activeSelection;
  }

  // Lazily create the transparent border-overlay layer once the first sheet has
  // rendered (needs the Univer canvas + skeleton to exist).
  private _ensureOverlay(): void {
    if (this.overlay || !this.univer) return;
    try {
      this.overlay = new CollabOverlay(this.host, {
        getUniver: () => this.univer,
        getUnitId: () => {
          try { return this.fWorkbook?.getId?.() || this.fWorkbook?.id || null; }
          catch (_e) { return null; }
        },
        // Inject the DI identifiers explicitly: a UMD/IIFE bundle never exposes
        // them on a global, so the overlay's name-based probe cannot find them.
        // Without these the projector fails to resolve the skeleton service and
        // the overlay silently draws nothing (the latent "no cursor" bug).
        identifiers: {
          IRenderManagerService: this.deps.IRenderManagerService,
          SheetSkeletonManagerService: this.deps.SheetSkeletonManagerService,
        },
      });
    } catch (_e) { this.overlay = null; }
  }

  // Lazily create the truncated-cell full-text bubble once the sheet has rendered
  // (needs the Univer canvas + hover service). Mirrors _ensureOverlay: the DI
  // identifiers are injected because a UMD bundle exposes none on a global.
  private _ensureCellTooltip(): void {
    if (this.cellTooltip || !this.univer) return;
    try {
      this.cellTooltip = new CellTooltip(this.host, {
        getUniver: () => this.univer,
        getUnitId: () => {
          try { return this.fWorkbook?.getId?.() || this.fWorkbook?.id || null; }
          catch (_e) { return null; }
        },
        getFSheet: () => (this.active ? this.active.fSheet : null),
        isSuppressed: () => this.editing,
        identifiers: {
          IRenderManagerService: this.deps.IRenderManagerService,
          SheetSkeletonManagerService: this.deps.SheetSkeletonManagerService,
          HoverManagerService: this.deps.HoverManagerService,
        },
      });
      this.cellTooltip.init();
    } catch (_e) { this.cellTooltip = null; }
  }

  // Remote peers' selections for the Univer engine (design §6.1). Univer is
  // canvas-rendered, so instead of mutating cell styles we draw each peer's
  // selection as a COLOURED BORDER on a transparent overlay above the canvas
  // (zero document pollution). Each peer's broadcast row uuids are mapped to the
  // rows that actually exist in THIS client's view (a peer's row missing here is
  // silently skipped), so differing sort/filter views never mis-place a box.
  // Returns true so the editor treats presence as handled here.
  setRemoteSelections(peers: Array<{
    key: string; name: string; color?: string;
    anchor?: { id: number; col: number } | null;
    rowIds: number[]; cols?: [number, number] | null;
  }>): boolean {
    const ctx = this.active;
    this._ensureOverlay();
    try {
      if ((globalThis as any).LM_DEBUG_CURSOR) {
        // eslint-disable-next-line no-console
        console.log("[adapter] setRemoteSelections in:", (peers || []).length,
          "ctx:", !!ctx, "overlay:", !!this.overlay, peers);
      }
    } catch (_e) { /* noop */ }
    if (!ctx || !this.overlay) return true;
    const vis = this._visibleFields(ctx);
    const rowByUuidId: Record<number, number> = {};
    ctx.items.forEach((it, idx) => { rowByUuidId[it.id as number] = idx; });

    const out: OverlaySelection[] = [];
    (peers || []).forEach((p) => {
      const c0 = p.cols ? Math.max(0, p.cols[0]) : 0;
      const c1 = p.cols ? Math.min(vis.length - 1, p.cols[1]) : vis.length - 1;
      const cells: Array<{ row: number; col: number }> = [];
      (p.rowIds || []).forEach((id) => {
        const idx = rowByUuidId[id];
        if (idx === undefined) return;               // row not in my view -> skip
        for (let col = c0; col <= c1; col++) cells.push({ row: HEADER_ROWS + idx, col });
      });
      let anchor: { row: number; col: number } | null = null;
      if (p.anchor) {
        const idx = rowByUuidId[p.anchor.id];
        if (idx !== undefined && p.anchor.col >= 0 && p.anchor.col < vis.length) {
          anchor = { row: HEADER_ROWS + idx, col: p.anchor.col };
        }
      }
      out.push({ key: p.key, name: p.name, color: p.color || "#888", anchor, cells });
    });
    this.overlay.setSelections(out);
    return true;
  }

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
              .setValue(this._toCellData(this._displayValue(b[f.field_key], f)));
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
        // clearContent keeps cell formatting, so any collaborator cell-error mark
        // from a previous render would linger on now-stale rows. Wipe the
        // data-region background too; error marks are re-applied afterwards by the
        // editor (applyCellErrors).
        try { clearRange.setBackgroundColor && clearRange.setBackgroundColor(null); } catch (_e) { /* optional */ }
        ctx.errCells = new Set();
      } catch (_e) { /* older facade: skip clear */ }

      if (ctx.items.length && vis.length) {
        const matrix = ctx.items.map((it) =>
          vis.map((f) => this._toCellData(this._displayValue(it[f.field_key], f))));
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
      // Multi-line rich-text cells make Univer auto-grow the row to fit every
      // line. We want uniform, fixed row heights instead. Forcing the height via
      // SetRowHeightCommand also flips each row's `ia` (isAutoHeight) flag to
      // FALSE, which permanently excludes the row from auto-height recalculation
      // (see engine-render calculateAutoHeightInRange). Re-applied on every full
      // render so a reload never leaves a stretched row behind.
      this._lockRowHeights(ctx);
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
    // Rows moved: reproject any remote-selection borders onto the new layout.
    if (this.overlay && ctx === this.active) this.overlay.refresh();
  }

  // Univer default row height (workbook is created without an override → 24px).
  private _rowHeightPx = 0;

  // Force every used row to a single fixed height and disable per-row
  // auto-height, so multi-line rich-text cells never stretch their row.
  private _lockRowHeights(ctx: SheetCtx): void {
    if (!ctx.fSheet || typeof ctx.fSheet.setRowHeightsForced !== "function") return;
    if (this._rowHeightPx <= 0) {
      let h = 24;
      try {
        const v = ctx.fSheet.getRowHeight && ctx.fSheet.getRowHeight(0);
        if (typeof v === "number" && v > 0) h = v;
      } catch (_e) { /* fall back to 24 */ }
      this._rowHeightPx = h;
    }
    // Cover the header + data region plus the clear buffer so freshly added
    // rows are locked too. Bounded by the sheet's max rows.
    let rows = HEADER_ROWS + ctx.items.length + 50;
    try {
      const max = ctx.fSheet.getMaxRows && ctx.fSheet.getMaxRows();
      if (typeof max === "number" && max > 0) rows = Math.min(rows, max);
    } catch (_e) { /* ignore */ }
    if (rows <= 0) return;
    try { ctx.fSheet.setRowHeightsForced(0, rows, this._rowHeightPx); }
    catch (_e) { /* best-effort: uniform row height not supported */ }
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

  // Wrap a display value for writing to Univer: multi-line strings become rich
  // text (cell.p) so the editor can load them; everything else stays a plain
  // primitive. setValues()/setValue() both accept ICellData, so this can be used
  // inline in a value matrix or for a single cell.
  private _toCellData(display: any): any {
    return toCellData(display);
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
          // Native "insert row" / "remove row" (context menu, toolbar) only mutate
          // Univer's in-memory grid — they create no backing server Item, so the
          // row is dropped on the next save/reload. Route them to the host's
          // create/delete flow (createItem(draft) / bulk-delete + reload) so the
          // change is persisted, then stop: they must not fall through to the
          // cell-diff sync below.
          if (this._maybeHandleStructural(id, cmd)) return;
          if (/paste|set-?range-?values|set-?range|fill|clear-?selection-?content|delete-?range/i.test(id)) {
            this._scheduleSync();
          } else if (/set-?worksheet-?active|active-?sheet|activate-?sheet/i.test(id)) {
            this._onActiveSheetMaybeChanged();
          }
          // Reproject the remote-cursor overlay whenever the grid GEOMETRY
          // changes: column width / row height (incl. their mutations + delta
          // form), scroll and zoom. The overlay reads cell coords from the LIVE
          // skeleton on every paint, so it never hardcodes sizes — it only needs
          // a repaint trigger. This makes a peer resizing a column (synced in via
          // a command with no local pointer event) update instantly instead of
          // waiting on the 250ms poll. Matches Univer 0.25.1 command ids:
          //   sheet.command/mutation.set-worksheet-col-width
          //   sheet.command/mutation.set-worksheet-row-height, delta-row-height
          //   sheet.operation.set-scroll, scroll-to-cell/range, scroll-view
          //   sheet.command/operation.set-zoom-ratio, change-zoom-ratio
          if (this.overlay &&
              /col-?width|row-?height|scroll|zoom/i.test(id)) {
            this.overlay.refresh();
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
    if (this.structuralPending) return;
    if (this.syncTimer) clearTimeout(this.syncTimer);
    this.syncTimer = setTimeout(() => { this.syncTimer = null; this._flushSync(); }, 90);
  }

  // Intercept Univer's native row-structure commands and route them to the host
  // callbacks that own server persistence. Matches ONLY the top-level command
  // ids (`sheet.command.insert-row` / `sheet.command.remove-row`) — never the
  // `sheet.mutation.*` ids — so each user action is handled exactly once.
  // Returns true when it consumed the command (caller then returns early).
  private _maybeHandleStructural(id: string, cmd: any): boolean {
    const ctx = this.active;
    if (!ctx) return false;
    const params = (cmd && cmd.params) || {};
    const range = params && params.range ? params.range : null;

    // --- insert row -------------------------------------------------------
    // InsertRowCommand's `range` is the block the NEW blank row(s) occupy, so
    // `range.startRow` is the absolute sheet row the blank now sits at. The item
    // that lived there (pre-insert; ctx.items is still the old layout) is the
    // anchor to insert ABOVE. Works for both "insert above" and "insert below"
    // without depending on params.direction. No item there → append at the end.
    if (/(?:^|\.)sheet\.command\.insert-row$/.test(id)) {
      if (!this.opts.onInsert) return false;
      const blankRow = range && typeof range.startRow === "number"
        ? range.startRow : ctx.items.length + HEADER_ROWS;
      const anchor = this._rowToItem(ctx, blankRow);
      this._beginStructural();
      try {
        if (anchor) {
          this.opts.onInsert(anchor, "above");
        } else {
          const last = ctx.items.length ? ctx.items[ctx.items.length - 1] : null;
          if (last) this.opts.onInsert(last, "below");
          else this.opts.onInsert(null as any, "below"); // empty sheet → append
        }
      } catch (e) {
        console.warn("[LMUniver] onInsert failed:", e);
        this._endStructural();
      }
      return true;
    }

    // --- remove row -------------------------------------------------------
    if (/(?:^|\.)sheet\.command\.remove-row$/.test(id)) {
      if (!range) return false;
      if (!this.opts.onBulkDelete && !this.opts.onDelete) return false;
      const s = Number(range.startRow);
      const e = Number(range.endRow);
      const ids: number[] = [];
      for (let r = s; r <= e; r++) {
        const it = this._rowToItem(ctx, r);
        if (it) ids.push(it.id as number);
      }
      if (!ids.length) return false;
      this._beginStructural();
      try {
        if (ids.length > 1 && this.opts.onBulkDelete) {
          this.opts.onBulkDelete(ids);
        } else if (this.opts.onDelete) {
          const first = this._rowToItem(ctx, s);
          if (first) this.opts.onDelete(first); else this._endStructural();
        } else if (this.opts.onBulkDelete) {
          this.opts.onBulkDelete(ids);
        } else {
          this._endStructural();
        }
      } catch (e) {
        console.warn("[LMUniver] onDelete failed:", e);
        this._endStructural();
      }
      return true;
    }

    return false;
  }

  // Enter the "structural change pending" state: suppress cell-diff saves and
  // drop any queued flush. A safety timeout lifts the guard even if the host
  // never calls setSheetData (e.g. the create/delete request failed), so saves
  // can never wedge permanently.
  private _beginStructural(): void {
    this.structuralPending = true;
    if (this.syncTimer) { clearTimeout(this.syncTimer); this.syncTimer = null; }
    if (this.structuralTimer) clearTimeout(this.structuralTimer);
    this.structuralTimer = setTimeout(() => {
      this.structuralPending = false;
      this.structuralTimer = null;
    }, 6000);
  }

  private _endStructural(): void {
    this.structuralPending = false;
    if (this.structuralTimer) { clearTimeout(this.structuralTimer); this.structuralTimer = null; }
  }

  // Diff the active worksheet's data region against its local cache and persist
  // every change, grouped per item (one PATCH per row). Handles single edits,
  // Delete-key clears, drag-fill and multi-cell paste uniformly.
  private async _flushSync(): Promise<void> {
    if (this.applying) return;
    // A native row insert/remove is in flight; the grid is intentionally out of
    // sync with ctx.items until the host reloads. Diffing now would mis-save the
    // shifted rows, so skip — setSheetData will clear the guard.
    if (this.structuralPending) return;
    if (this.syncing) { this._scheduleSync(); return; }
    const ctx = this.active;
    if (!ctx) return;
    const vis = this._visibleFields(ctx);
    if (!ctx.fSheet || !vis.length || !ctx.items.length) return;
    if (!this.opts.onSave) return;

    // Read with rich-text awareness: a multi-line cell (stored as cell.p) comes
    // back as a RichTextValue, not a primitive. cellReadToText() flattens it to
    // plain text with normalized "\n" so the diff below sees the SAME string that
    // is cached — otherwise merely clicking a newline cell (which Univer promotes
    // to rich text) would read back a value that differs from the cache and be
    // mistaken for an edit, silently rewriting the cell and dropping the newline.
    let values: any[][];
    try {
      const range = ctx.fSheet.getRange(HEADER_ROWS, 0, ctx.items.length, vis.length);
      const raw = (range.getValueAndRichTextValues
        ? range.getValueAndRichTextValues()
        : range.getValues()) || [];
      values = raw.map((row: any[]) => (row || []).map((cell) => cellReadToText(cell)));
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
    try { ctx.fSheet.getRange(row, col, 1, 1).setValue(this._toCellData(display)); }
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
    const rowIds: number[] = [];
    let minCol = Infinity;
    let maxCol = -Infinity;
    ranges.forEach((r) => {
      const s = r.startRow ?? r.row;
      const e = r.endRow ?? s;
      const sc = r.startColumn ?? r.column ?? r.col;
      const ec = r.endColumn ?? sc;
      if (s == null) return;
      for (let row = Math.max(s, HEADER_ROWS); row <= e; row++) {
        const it = this._rowToItem(ctx, row);
        if (it) { ctx.selected.add(it.id); rowIds.push(it.id as number); }
      }
      if (sc != null) { minCol = Math.min(minCol, Number(sc)); maxCol = Math.max(maxCol, Number(ec ?? sc)); }
      if (s === e && sc != null && sc === ec) single = { row: Number(s), col: Number(sc) };
    });
    // Record the active cell so the editor can publish it as the anchor
    // (design §6.1); col is the visible-column index, matching FallbackGrid.
    if (single) {
      const it = this._rowToItem(ctx, (single as { row: number; col: number }).row);
      this.activeCell = it ? { id: it.id as number, col: (single as { row: number; col: number }).col } : null;
    } else {
      this.activeCell = null;
    }
    // Record the full selection so the editor can publish a border overlay for
    // peers: anchor + selected row ids + visible column span (design §6.1).
    this.activeSelection = {
      anchor: this.activeCell,
      rowIds,
      cols: minCol <= maxCol ? [minCol, maxCol] : null,
    };
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

