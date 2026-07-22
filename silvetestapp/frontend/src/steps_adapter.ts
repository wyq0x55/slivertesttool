/*
 * UniverStepsView — renders one test item's "steps" document as Univer Sheets,
 * as a drop-in replacement for the built-in step-detail tables
 * (js/lanmatrix/steps_editor.js — the "操作步骤明细" editor).
 *
 * Document shape (identical to the Test-Matrix codec):
 *   { input_signals:  [[name, path], ...],
 *     expected_signals:[[name, path], ...],
 *     steps: [{ no, purpose, operation, subroutine, args,
 *               inputs:[...], expecteds:[...], timing }, ...] }
 *
 * It exposes the tiny contract steps_editor.js needs:
 *   mount(host, opts) -> { setDoc(doc), getDoc(): doc, engine }
 * steps_editor.js keeps ownership of the toolbar (add input/expected/step) and
 * of persistence: on save it pulls getDoc(), serialises and PATCHes the item's
 * `steps` cell through the normal optimistic-locked item API. Server stays
 * authoritative; this is purely an editing surface.
 *
 * Layout: three worksheets in one workbook —
 *   "入力値"  : columns [名称, 路径]
 *   "期待値"  : columns [名称, 路径]
 *   "手順"    : [手順番号, 手順目的, 操作手順, サブルーチン, 引数,
 *               <one column per input signal>, <one per expected signal>,
 *               確認タイミング]
 */

import type { UniverDeps } from "./adapter";
import { toCellData, cellReadToText } from "./adapter";
import { CellTooltip } from "./cell_tooltip";

export interface StepDoc {
  input_signals: any[];
  expected_signals: any[];
  steps: any[];
}

const SHEET_IN = "入力値";
const SHEET_EX = "期待値";
const SHEET_STEP = "手順";
const STEP_LEFT = ["手順番号", "手順目的", "操作手順", "サブルーチン", "引数"];
const STEP_RIGHT = ["確認タイミング"];
const SCAN_ROWS = 500; // upper bound when scanning a sheet back into a doc

function s(v: any): string {
  return v == null ? "" : String(v);
}

export class UniverStepsView {
  engine = "univer";

  private deps: UniverDeps;
  private host: HTMLElement;
  private univer: any = null;   // Univer instance (for the tooltip projector)
  private univerAPI: any = null;
  private fWorkbook: any = null;
  private ni = 0; // current input-signal count (defines step column geometry)
  private ne = 0; // current expected-signal count
  private hasValidation = false; // whether the data-validation preset is available
  private cellTooltip: CellTooltip | null = null; // truncated-cell full-text bubble
  private _rowHeightPx = 0; // captured uniform row height (default 24px)
  // Lib reference map (lib_func lowercased -> definition) for the サブルーチン
  // hover tooltip (feature C). Populated by setRefData() from the host.
  private _libMap: Map<string, { func: string; name: string; arg: string; note: string }> | null = null;

  constructor(deps: UniverDeps, host: HTMLElement) {
    this.deps = deps;
    this.host = host;
  }

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
    if (!this.host.style.height) this.host.style.height = "60vh";
    this.host.classList.add("lm-univer-steps-host");

    // Feature parity with the main matrix: register the SAME preset set so the
    // step-detail view also gets data validation, conditional formatting, filter,
    // sort and find/replace — not just the core grid + table. All locales are
    // Simplified Chinese (zh-CN).
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

    const { univer, univerAPI } = createUniver({
      locale: LocaleType.ZH_CN,
      locales: { [LocaleType.ZH_CN]: mergeLocales(...locales) },
      theme: defaultTheme,
      presets,
    });
    this.univer = univer;
    this.univerAPI = univerAPI;

    // Crosshair highlight (active row/column) — same plugin as the main grid.
    if (univer && UniverSheetsCrosshairHighlightPlugin) {
      try { univer.registerPlugin(UniverSheetsCrosshairHighlightPlugin); }
      catch (_e) { /* best-effort */ }
    }
    try {
      if (typeof univerAPI.setCrosshairHighlightEnabled === "function") {
        univerAPI.setCrosshairHighlightEnabled(true);
        if (typeof univerAPI.setCrosshairHighlightColor === "function") {
          univerAPI.setCrosshairHighlightColor("rgba(59,130,246,0.14)");
        }
      }
    } catch (_e) { /* facade absent → highlight stays off */ }

    // Mount the truncated-cell full-text bubble once the sheet canvas has
    // rendered (same feature the main grid has). Best-effort: if the lifecycle
    // event or hover service is missing, the bubble simply never shows.
    try {
      const ev = univerAPI.Event;
      if (ev && ev.LifeCycleChanged && typeof univerAPI.addEvent === "function") {
        univerAPI.addEvent(ev.LifeCycleChanged, (p: any) => {
          const stage = p && p.stage;
          const rendered = univerAPI.Enum && univerAPI.Enum.LifecycleStages
            ? univerAPI.Enum.LifecycleStages.Rendered : undefined;
          if (rendered === undefined || stage === rendered) this._ensureCellTooltip();
        });
      }
    } catch (_e) { /* tooltip is best-effort */ }

    this.fWorkbook = univerAPI.createWorkbook({ name: "Steps" });
  }

  // Lazily create the truncated-cell full-text bubble (needs the Univer canvas +
  // hover service). Mirrors the main grid's _ensureCellTooltip: the DI
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
        getFSheet: () => {
          try { return this.fWorkbook ? this.fWorkbook.getActiveSheet() : null; }
          catch (_e) { return null; }
        },
        lookupRef: (row: number, col: number) => this._lookupLib(row, col),
        identifiers: {
          IRenderManagerService: this.deps.IRenderManagerService,
          SheetSkeletonManagerService: this.deps.SheetSkeletonManagerService,
          HoverManagerService: this.deps.HoverManagerService,
        },
      });
      this.cellTooltip.init();
    } catch (_e) { this.cellTooltip = null; }
  }

  // Feature C: receive the current project's Lib rows so hovering a サブルーチン
  // cell whose text matches a lib_func reveals that function's definition. Called
  // by steps_editor.js after it loads the reference data. const is ignored here
  // (lib is always used in the サブルーチン column per the workflow).
  setRefData(data: any): void {
    const lib = (data && data.lib) || [];
    const m = new Map<string, { func: string; name: string; arg: string; note: string }>();
    for (const row of lib) {
      const func = s(row && row.lib_func).trim();
      if (!func) continue;
      m.set(func.toLowerCase(), {
        func,
        name: s(row && row.lib_name).trim(),
        arg: s(row && row.lib_arg).trim(),
        note: s(row && row.lib_note).trim(),
      });
    }
    this._libMap = m;
  }

  // Return a formatted Lib definition when (row,col) is a サブルーチン cell on the
  // 手順 sheet whose text matches a known lib_func; else null (tooltip then falls
  // back to its normal truncated-text behaviour).
  private _lookupLib(row: number, col: number): string | null {
    if (!this._libMap || !this._libMap.size) return null;
    if (row < 1) return null; // header row
    if (col !== STEP_LEFT.indexOf("サブルーチン")) return null;
    try {
      const active = this.fWorkbook && this.fWorkbook.getActiveSheet
        ? this.fWorkbook.getActiveSheet() : null;
      if (!active) return null;
      const name = typeof active.getSheetName === "function" ? active.getSheetName() : null;
      if (name !== SHEET_STEP) return null;
      const rng = active.getRange(row, col);
      let raw = rng && rng.getValue ? rng.getValue() : "";
      raw = s(raw).trim();
      if (!raw) return null;
      // Tolerate an author-typed call suffix, e.g. "SetSignal(a, b)".
      const paren = raw.indexOf("(");
      const key = (paren > 0 ? raw.slice(0, paren) : raw).trim().toLowerCase();
      const def = this._libMap.get(key) || this._libMap.get(raw.toLowerCase());
      if (!def) return null;
      const lines: string[] = [def.func];
      if (def.name) lines.push("名称: " + def.name);
      if (def.arg) lines.push("引数: " + def.arg);
      if (def.note) lines.push(def.note);
      return lines.join("\n");
    } catch (_e) { return null; }
  }

  // Style a written region like a table WITHOUT using a Univer "table" object:
  // bold header, light header background, and freeze the header row. This is all
  // synchronous cell/worksheet formatting (unlike createTable/removeTable, which
  // are async and — because a table's baked-in range + filter state survive a
  // rewrite — were the root cause of the step detail not refreshing when the
  // reused view switched to another item). Filter/sort are still available from
  // the toolbar via their presets.
  private _styleHeader(sheet: any, cols: number): void {
    if (!sheet || cols < 1) return;
    try {
      const hdr = sheet.getRange(0, 0, 1, cols);
      try { hdr.setFontWeight && hdr.setFontWeight("bold"); } catch (_e) { /* optional */ }
      try { hdr.setBackgroundColor && hdr.setBackgroundColor("#eef1f8"); } catch (_e) { /* optional */ }
    } catch (_e) { /* best-effort */ }
    try {
      if (typeof sheet.setFreeze === "function") {
        sheet.setFreeze({ xSplit: 0, ySplit: 1, startRow: 1, startColumn: 0 });
      } else if (typeof sheet.setFrozenRows === "function") {
        sheet.setFrozenRows(1);
      }
    } catch (_e) { /* freeze is best-effort */ }
  }

  // --------------------------------------------------------------- worksheets -
  private _sheet(name: string, rows: number, cols: number): any {
    let sh: any = null;
    try { sh = this.fWorkbook.getSheetByName(name); } catch (_e) { /* ignore */ }
    if (sh) return sh;
    // Reuse the default sheet for the first requested one, then create more.
    try {
      const active = this.fWorkbook.getActiveSheet();
      if (active && typeof active.setName === "function" &&
          !this._named(SHEET_IN) && !this._named(SHEET_EX) && !this._named(SHEET_STEP)) {
        active.setName(name);
        return active;
      }
    } catch (_e) { /* ignore */ }
    try { return this.fWorkbook.create(name, rows, cols); } catch (_e) { /* try next */ }
    try { return this.fWorkbook.insertSheet(name); } catch (_e) { /* give up */ }
    return null;
  }

  private _named(name: string): any {
    try { return this.fWorkbook.getSheetByName(name); } catch (_e) { return null; }
  }

  private _write(sheet: any, matrix: any[][]): void {
    if (!sheet || !matrix.length) return;
    const rows = matrix.length;
    const cols = Math.max(...matrix.map((r) => r.length), 1);
    const wide = Math.max(cols + 4, 40);
    // Grow the (possibly reused) sheet so neither the clear band nor the write
    // exceeds its bounds. When the view is reused and the next item has more
    // signal columns than the sheet was created with, getRange() throws
    // "Range is out of bounds" and the whole write aborts — leaving the view
    // showing the previous item. Widen synchronously first.
    this._ensureSize(sheet, SCAN_ROWS, wide);
    // Clear a generously wide band so a narrower doc never leaves the previous
    // (wider) doc's trailing columns behind when the view is reused.
    try { sheet.getRange(0, 0, SCAN_ROWS, wide).clearContent(); } catch (_e) { /* best-effort */ }
    // Multi-line values must be written as rich text (cell.p) or the cell editor
    // opens blank on a value with an embedded newline — same conversion the main
    // grid uses (toCellData). setValues accepts ICellData[][], so this inlines.
    const cellMatrix = matrix.map((r) => r.map((v) => toCellData(v)));
    try { sheet.getRange(0, 0, rows, cols).setValues(cellMatrix); } catch (e) {
      console.warn("[LMUniverSteps] write failed:", e);
    }
    try { sheet.getRange(0, 0, 1, cols).setFontWeight("bold"); } catch (_e) { /* optional */ }
    // Keep uniform row height: rich-text cells otherwise auto-grow their row.
    this._lockRowHeights(sheet, rows);
  }

  // Ensure the sheet is at least `rows` x `cols` big, growing a reused sheet
  // synchronously via setRowCount / setColumnCount (both run through
  // syncExecuteCommand) so subsequent getRange() calls never go out of bounds.
  private _ensureSize(sheet: any, rows: number, cols: number): void {
    if (!sheet) return;
    try {
      const maxC = sheet.getMaxColumns && sheet.getMaxColumns();
      if (typeof maxC === "number" && cols > maxC &&
          typeof sheet.setColumnCount === "function") {
        sheet.setColumnCount(cols);
      }
    } catch (_e) { /* best-effort */ }
    try {
      const maxR = sheet.getMaxRows && sheet.getMaxRows();
      if (typeof maxR === "number" && rows > maxR &&
          typeof sheet.setRowCount === "function") {
        sheet.setRowCount(rows);
      }
    } catch (_e) { /* best-effort */ }
  }

  // Force every used row to a single fixed height and disable per-row
  // auto-height, so multi-line rich-text cells never stretch their row. Forcing
  // the height via SetRowHeightCommand also flips each row's `ia` (isAutoHeight)
  // flag to FALSE, permanently excluding it from auto-height recalculation.
  private _lockRowHeights(sheet: any, rows: number): void {
    if (!sheet || typeof sheet.setRowHeightsForced !== "function") return;
    if (this._rowHeightPx <= 0) {
      let h = 24;
      try {
        const v = sheet.getRowHeight && sheet.getRowHeight(0);
        if (typeof v === "number" && v > 0) h = v;
      } catch (_e) { /* fall back to 24 */ }
      this._rowHeightPx = h;
    }
    let n = Math.max(rows, 1) + 50;
    try {
      const max = sheet.getMaxRows && sheet.getMaxRows();
      if (typeof max === "number" && max > 0) n = Math.min(n, max);
    } catch (_e) { /* ignore */ }
    try { sheet.setRowHeightsForced(0, n, this._rowHeightPx); }
    catch (_e) { /* best-effort */ }
  }

  private _readMatrix(sheet: any): any[][] {
    if (!sheet) return [];
    // Prefer a rich-text-aware read so a multi-line cell round-trips with its
    // newlines intact (a plain getValues() flattens rich text and loses them).
    const flatten = (vals: any[][]): any[][] =>
      vals.map((row) => (row || []).map((c) => cellReadToText(c)));
    try {
      const dr = sheet.getDataRange();
      if (dr && typeof dr.getValueAndRichTextValues === "function") {
        const rv = dr.getValueAndRichTextValues();
        if (rv) return flatten(rv);
      }
      const vals = dr && typeof dr.getValues === "function" ? dr.getValues() : null;
      if (vals) return vals;
    } catch (_e) { /* fall through to fixed scan */ }
    try {
      const rng = sheet.getRange(0, 0, SCAN_ROWS, 40);
      if (typeof rng.getValueAndRichTextValues === "function") {
        return flatten(rng.getValueAndRichTextValues() || []);
      }
      return rng.getValues() || [];
    } catch (_e) { return []; }
  }

  // ------------------------------------------------------------------ setDoc --
  setDoc(doc: StepDoc): void {
    const inSig = Array.isArray(doc.input_signals) ? doc.input_signals : [];
    const exSig = Array.isArray(doc.expected_signals) ? doc.expected_signals : [];
    const steps = Array.isArray(doc.steps) ? doc.steps : [];
    this.ni = inSig.length;
    this.ne = exSig.length;
    try {
      if ((globalThis as any).LM_DEBUG_STEPS) {
        // eslint-disable-next-line no-console
        console.log("[steps] setDoc", { ni: this.ni, ne: this.ne, steps: steps.length });
      }
    } catch (_e) { /* noop */ }

    // 入力値 / 期待値 sheets: header + [name, path] rows.
    const inMatrix = [["名称", "路径"]].concat(
      inSig.map((sig: any) => [s(sig && sig[0]), s(sig && sig[1])]));
    const exMatrix = [["名称", "路径"]].concat(
      exSig.map((sig: any) => [s(sig && sig[0]), s(sig && sig[1])]));

    // 手順 sheet: dynamic signal columns labelled by signal name.
    const inHead = inSig.map((sig: any, i: number) => `入力: ${s(sig && sig[0]) || "入力" + (i + 1)}`);
    const exHead = exSig.map((sig: any, i: number) => `期待: ${s(sig && sig[0]) || "期待" + (i + 1)}`);
    const header = STEP_LEFT.concat(inHead, exHead, STEP_RIGHT);
    const stepMatrix = [header].concat(steps.map((st: any) => {
      const inputs = Array.isArray(st.inputs) ? st.inputs : [];
      const exps = Array.isArray(st.expecteds) ? st.expecteds : [];
      const row = [s(st.no), s(st.purpose), s(st.operation), s(st.subroutine), s(st.args)];
      for (let i = 0; i < this.ni; i++) row.push(s(inputs[i]));
      for (let i = 0; i < this.ne; i++) row.push(s(exps[i]));
      row.push(s(st.timing));
      return row;
    }));

    // The view is REUSED across items (not disposed/recreated per open — that was
    // slow and sometimes failed to display). A Univer "table" object baked in by
    // a previous item keeps its own range + filter state across a rewrite, which
    // hid/misplaced the new item's rows (the "content not refreshing" bug). Drop
    // every table so each item is rendered as plain, fully-rewritten ranges.
    this._resetTables();

    const shIn = this._sheet(SHEET_IN, SCAN_ROWS, 4);
    const shEx = this._sheet(SHEET_EX, SCAN_ROWS, 4);
    const shStep = this._sheet(SHEET_STEP, SCAN_ROWS, header.length + 2);
    this._write(shIn, inMatrix);
    this._write(shEx, exMatrix);
    this._write(shStep, stepMatrix);

    // Table-like styling without a Univer table object (synchronous; see
    // _styleHeader): bold + shaded header, frozen header row.
    this._styleHeader(shIn, 2);
    this._styleHeader(shEx, 2);
    this._styleHeader(shStep, header.length);

    // Land on the 手順 sheet so a reused view never shows the previous item's
    // active sheet.
    try {
      const st = this._named(SHEET_STEP);
      if (st && typeof st.activate === "function") st.activate();
    } catch (_e) { /* best-effort */ }
  }

  // Remove every table in the workbook. The step view no longer auto-creates
  // tables (see _styleHeader), but a reused instance from before this change — or
  // one where a user manually inserted a table — could still carry one whose
  // stale range/filter hides the freshly written rows. Best-effort.
  private _resetTables(): void {
    if (!this.fWorkbook || typeof this.fWorkbook.getTableList !== "function") return;
    try {
      const list = this.fWorkbook.getTableList();
      if (Array.isArray(list)) {
        list.forEach((t: any) => {
          const id = t && (t.id || t.tableId);
          if (id && typeof this.fWorkbook.removeTable === "function") {
            try { this.fWorkbook.removeTable(id); } catch (_e) { /* ignore */ }
          }
        });
      }
    } catch (_e) { /* best-effort */ }
  }

  // Nudge Univer to re-lay its canvas after the dialog container resizes
  // (fullscreen toggle). steps_editor.js calls this if present.
  resize(): void {
    try {
      if (this.univerAPI && typeof this.univerAPI.getActiveWorkbook === "function") {
        // A no-op scroll/refresh is enough to force a relayout in most builds;
        // fall back to a window resize event which Univer also listens to.
      }
    } catch (_e) { /* ignore */ }
    try { window.dispatchEvent(new Event("resize")); } catch (_e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ getDoc --
  getDoc(): StepDoc {
    const inSig = this._readSignals(this._named(SHEET_IN));
    const exSig = this._readSignals(this._named(SHEET_EX));
    this.ni = inSig.length;
    this.ne = exSig.length;

    const rows = this._readMatrix(this._named(SHEET_STEP));
    const steps: any[] = [];
    for (let r = 1; r < rows.length; r++) {
      const row = rows[r] || [];
      const no = s(row[0]);
      const purpose = s(row[1]);
      const operation = s(row[2]);
      const subroutine = s(row[3]);
      const args = s(row[4]);
      const inputs: string[] = [];
      const expecteds: string[] = [];
      let base = 5;
      for (let i = 0; i < this.ni; i++) inputs.push(s(row[base + i]));
      base += this.ni;
      for (let i = 0; i < this.ne; i++) expecteds.push(s(row[base + i]));
      base += this.ne;
      const timing = s(row[base]);
      // Skip fully-empty trailing rows.
      if (!no && !purpose && !operation && !subroutine && !args && !timing &&
          inputs.every((v) => !v) && expecteds.every((v) => !v)) continue;
      steps.push({ no, purpose, operation, subroutine, args, inputs, expecteds, timing });
    }
    return { input_signals: inSig, expected_signals: exSig, steps };
  }

  // Release the Univer instance backing this view. The view is now REUSED across
  // items (setDoc fully resets geometry), so this is only called on a genuine
  // teardown — not on every dialog close — but kept correct for that case.
  dispose(): void {
    try { this.cellTooltip?.dispose?.(); } catch (_e) { /* best-effort */ }
    this.cellTooltip = null;
    try {
      if (this.univerAPI && typeof this.univerAPI.dispose === "function") {
        this.univerAPI.dispose();
      }
    } catch (_e) { /* best-effort teardown */ }
    this.fWorkbook = null;
    this.univerAPI = null;
    this.univer = null;
    this._rowHeightPx = 0;
  }

  private _readSignals(sheet: any): any[][] {
    const rows = this._readMatrix(sheet);
    const out: any[][] = [];
    for (let r = 1; r < rows.length; r++) {
      const row = rows[r] || [];
      const name = s(row[0]);
      const path = s(row[1]);
      if (!name && !path) continue; // drop blank rows
      out.push([name, path]);
    }
    return out;
  }
}

export function createStepsMount(deps: UniverDeps) {
  return function mount(host: HTMLElement, _opts?: any): UniverStepsView {
    const view = new UniverStepsView(deps, host);
    view.init();
    return view;
  };
}
