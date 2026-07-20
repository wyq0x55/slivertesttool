/*
 * Entry point for the Univer Sheets bundle.
 *
 * Vite compiles this (with all Univer code inlined) into
 *   app/static/vendor/univer/univer.full.umd.js  (+ .css)
 * and it assigns `window.LMUniver`. The LAN Test Matrix editor's grid.js
 * auto-detects `window.LMUniver.mount` and uses Univer instead of the built-in
 * grid — same data contract, same callbacks, no backend change.
 *
 * Localization policy: the whole Univer UI (menus, dialogs, tooltips, table /
 * crosshair-highlight feature strings) ships in Simplified Chinese (zh-CN).
 * Every preset — current and future — MUST be registered with its zh-CN locale
 * bundle and the workbook created with LocaleType.ZH_CN.
 */
import { createUniver, defaultTheme, LocaleType, mergeLocales } from "@univerjs/presets";
import { UniverSheetsCorePreset } from "@univerjs/preset-sheets-core";
import UniverPresetSheetsCoreZhCN from "@univerjs/preset-sheets-core/locales/zh-CN";
import "@univerjs/preset-sheets-core/lib/index.css";

// Data validation preset: enables real dropdowns (single/multi-select, boolean)
// and the date picker inside cells. Without it those field types render as plain
// text. Its CSS must load too or the dropdown/date UI is unstyled.
import { UniverSheetsDataValidationPreset } from "@univerjs/preset-sheets-data-validation";
import UniverPresetSheetsDataValidationZhCN from "@univerjs/preset-sheets-data-validation/locales/zh-CN";
import "@univerjs/preset-sheets-data-validation/lib/index.css";

// Filter preset: adds Excel-style column filter buttons to the header row so the
// matrix can be filtered in place. Its CSS is required for the filter panel UI.
import { UniverSheetsFilterPreset } from "@univerjs/preset-sheets-filter";
import UniverPresetSheetsFilterZhCN from "@univerjs/preset-sheets-filter/locales/zh-CN";
import "@univerjs/preset-sheets-filter/lib/index.css";

// Find & Replace preset: adds the toolbar entry + Ctrl+F search/replace dialog.
import { UniverSheetsFindReplacePreset } from "@univerjs/preset-sheets-find-replace";
import UniverPresetSheetsFindReplaceZhCN from "@univerjs/preset-sheets-find-replace/locales/zh-CN";
import "@univerjs/preset-sheets-find-replace/lib/index.css";

// Sort preset: adds the toolbar/menu "Sort" entries so any column range can be
// sorted ascending/descending in place.
import { UniverSheetsSortPreset } from "@univerjs/preset-sheets-sort";
import UniverPresetSheetsSortZhCN from "@univerjs/preset-sheets-sort/locales/zh-CN";
import "@univerjs/preset-sheets-sort/lib/index.css";

// Conditional-formatting preset: adds the rule manager (color scales, data bars,
// highlight rules, etc.) to the toolbar/menu. Its CSS renders the rule dialog.
import { UniverSheetsConditionalFormattingPreset } from "@univerjs/preset-sheets-conditional-formatting";
import UniverPresetSheetsConditionalFormattingZhCN from "@univerjs/preset-sheets-conditional-formatting/locales/zh-CN";
import "@univerjs/preset-sheets-conditional-formatting/lib/index.css";

// Table preset: turns a range into a first-class Univer "table" (banded rows,
// header row, built-in filter/sort). The step-detail view uses it to render the
// 入力値 / 期待値 / 手順 regions as real tables instead of a separate popup
// dialog, and it is also available in the main matrix toolbar. CSS required.
import { UniverSheetsTablePreset } from "@univerjs/preset-sheets-table";
import UniverPresetSheetsTableZhCN from "@univerjs/preset-sheets-table/locales/zh-CN";
import "@univerjs/preset-sheets-table/lib/index.css";

// Crosshair highlight plugin: highlights the row + column of the active cell so
// large matrices stay readable. This is a standalone plugin (not a preset), so
// it is registered on the `univer` instance and enabled through its facade
// extension (adds univerAPI.setCrosshairHighlightEnabled / ...Color).
import { UniverSheetsCrosshairHighlightPlugin } from "@univerjs/sheets-crosshair-highlight";
import SheetsCrosshairHighlightZhCN from "@univerjs/sheets-crosshair-highlight/locale/zh-CN";
import "@univerjs/sheets-crosshair-highlight/lib/index.css";
import "@univerjs/sheets-crosshair-highlight/facade";

import { UniverGridAdapter, MountOpts } from "./adapter";
import { UniverStepsView, createStepsMount } from "./steps_adapter";

declare global {
  interface Window {
    LMUniver?: { mount(opts: MountOpts): UniverGridAdapter };
    LMUniverSteps?: { mount(host: HTMLElement, opts?: any): UniverStepsView };
  }
}

const deps = {
  createUniver, defaultTheme, LocaleType, mergeLocales,
  UniverSheetsCorePreset, UniverPresetSheetsCoreZhCN,
  UniverSheetsDataValidationPreset, UniverPresetSheetsDataValidationZhCN,
  UniverSheetsFilterPreset, UniverPresetSheetsFilterZhCN,
  UniverSheetsFindReplacePreset, UniverPresetSheetsFindReplaceZhCN,
  UniverSheetsSortPreset, UniverPresetSheetsSortZhCN,
  UniverSheetsConditionalFormattingPreset, UniverPresetSheetsConditionalFormattingZhCN,
  UniverSheetsTablePreset, UniverPresetSheetsTableZhCN,
  UniverSheetsCrosshairHighlightPlugin, SheetsCrosshairHighlightZhCN,
};

// Test Matrix editor grid.
window.LMUniver = {
  mount(opts: MountOpts): UniverGridAdapter {
    const adapter = new UniverGridAdapter(deps, opts);
    adapter.init();
    return adapter;
  },
};

// "操作步骤明细" (step-detail) editor.
window.LMUniverSteps = {
  mount: createStepsMount(deps),
};
