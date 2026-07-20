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
  private univerAPI: any = null;
  private fWorkbook: any = null;
  private ni = 0; // current input-signal count (defines step column geometry)
  private ne = 0; // current expected-signal count
  private hasTable = false; // whether the table preset is available
  private tabledSheets = new Set<string>(); // sheets already promoted to a table

  constructor(deps: UniverDeps, host: HTMLElement) {
    this.deps = deps;
    this.host = host;
  }

  init(): void {
    const { createUniver, defaultTheme, LocaleType, mergeLocales,
            UniverSheetsCorePreset, UniverPresetSheetsCoreZhCN,
            UniverSheetsTablePreset, UniverPresetSheetsTableZhCN,
            UniverSheetsCrosshairHighlightPlugin, SheetsCrosshairHighlightZhCN } = this.deps;
    if (!this.host.style.height) this.host.style.height = "60vh";
    this.host.classList.add("lm-univer-steps-host");

    // All locales Simplified Chinese (zh-CN). The table preset renders the
    // 入力値 / 期待値 / 手順 regions as first-class Univer tables — the detailed
    // steps are shown as tables here instead of a separate pop-up dialog.
    const presets: any[] = [UniverSheetsCorePreset({ container: this.host })];
    const locales: any[] = [UniverPresetSheetsCoreZhCN];
    if (typeof UniverSheetsTablePreset === "function") {
      presets.push(UniverSheetsTablePreset());
      if (UniverPresetSheetsTableZhCN) locales.push(UniverPresetSheetsTableZhCN);
      this.hasTable = true;
    }
    if (SheetsCrosshairHighlightZhCN) locales.push(SheetsCrosshairHighlightZhCN);

    const { univer, univerAPI } = createUniver({
      locale: LocaleType.ZH_CN,
      locales: { [LocaleType.ZH_CN]: mergeLocales(...locales) },
      theme: defaultTheme,
      presets,
    });
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

    this.fWorkbook = univerAPI.createWorkbook({ name: "Steps" });
  }

  // Promote a written data region (header row + rows) to a first-class Univer
  // table so the step detail reads as a real table (banded rows, header
  // filter/sort) rather than a plain range. Best-effort and idempotent per
  // sheet: probes the range- and worksheet-level table factories the running
  // Univer build exposes and quietly no-ops if the table preset is absent.
  private _applyTable(sheet: any, name: string, rows: number, cols: number): void {
    if (!sheet || !this.hasTable || rows < 2 || cols < 1) return;
    if (this.tabledSheets.has(name)) return; // already a table; range auto-tracks edits
    try {
      const range = sheet.getRange(0, 0, rows, cols);
      if (range && typeof range.createTable === "function") {
        range.createTable();
        this.tabledSheets.add(name);
        return;
      }
      const a1 = range && typeof range.getA1Notation === "function" ? range.getA1Notation() : null;
      if (a1 && typeof sheet.addTable === "function") {
        sheet.addTable(name, a1);
        this.tabledSheets.add(name);
        return;
      }
      if (a1 && typeof sheet.insertTable === "function") {
        sheet.insertTable(name, a1);
        this.tabledSheets.add(name);
      }
    } catch (_e) { /* table styling is best-effort */ }
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
    // Clear a generously wide band so a narrower doc never leaves the previous
    // (wider) doc's trailing columns behind when the view is reused.
    try { sheet.getRange(0, 0, SCAN_ROWS, Math.max(cols + 4, 40)).clearContent(); } catch (_e) { /* best-effort */ }
    try { sheet.getRange(0, 0, rows, cols).setValues(matrix); } catch (e) {
      console.warn("[LMUniverSteps] write failed:", e);
    }
    try { sheet.getRange(0, 0, 1, cols).setFontWeight("bold"); } catch (_e) { /* optional */ }
  }

  private _readMatrix(sheet: any): any[][] {
    if (!sheet) return [];
    try {
      const dr = sheet.getDataRange();
      const vals = dr && typeof dr.getValues === "function" ? dr.getValues() : null;
      if (vals) return vals;
    } catch (_e) { /* fall through to fixed scan */ }
    try { return sheet.getRange(0, 0, SCAN_ROWS, 40).getValues() || []; }
    catch (_e) { return []; }
  }

  // ------------------------------------------------------------------ setDoc --
  setDoc(doc: StepDoc): void {
    const inSig = Array.isArray(doc.input_signals) ? doc.input_signals : [];
    const exSig = Array.isArray(doc.expected_signals) ? doc.expected_signals : [];
    const steps = Array.isArray(doc.steps) ? doc.steps : [];
    this.ni = inSig.length;
    this.ne = exSig.length;

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

    const shIn = this._sheet(SHEET_IN, SCAN_ROWS, 4);
    const shEx = this._sheet(SHEET_EX, SCAN_ROWS, 4);
    const shStep = this._sheet(SHEET_STEP, SCAN_ROWS, header.length + 2);
    this._write(shIn, inMatrix);
    this._write(shEx, exMatrix);
    this._write(shStep, stepMatrix);

    // Render each region as a first-class Univer table (see _applyTable).
    this._applyTable(shIn, SHEET_IN, inMatrix.length, 2);
    this._applyTable(shEx, SHEET_EX, exMatrix.length, 2);
    this._applyTable(shStep, SHEET_STEP, stepMatrix.length, header.length);
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

  // Release the Univer instance backing this view. steps_editor.js calls this
  // when the dialog closes / before opening another item, so each item gets a
  // fresh workbook instead of inheriting the previous one's sheet geometry.
  dispose(): void {
    try {
      if (this.univerAPI && typeof this.univerAPI.dispose === "function") {
        this.univerAPI.dispose();
      }
    } catch (_e) { /* best-effort teardown */ }
    this.fWorkbook = null;
    this.univerAPI = null;
    this.tabledSheets.clear();
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
