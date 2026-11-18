import { UniverSheetsDataValidationPreset } from "@univerjs/preset-sheets-data-validation";
import UniverPresetSheetsDataValidationZhCN from "@univerjs/preset-sheets-data-validation/locales/zh-CN";
import { UniverSheetsFindReplacePreset } from "@univerjs/preset-sheets-find-replace";
import UniverPresetSheetsFindReplaceZhCN from "@univerjs/preset-sheets-find-replace/locales/zh-CN";
import { UniverSheetsConditionalFormattingPreset } from "@univerjs/preset-sheets-conditional-formatting";
import UniverPresetSheetsConditionalFormattingZhCN from "@univerjs/preset-sheets-conditional-formatting/locales/zh-CN";
import type { UniverDeps } from "./adapter";

export const secondaryDeps: Partial<UniverDeps> = {
  UniverSheetsDataValidationPreset,
  UniverPresetSheetsDataValidationZhCN,
  UniverSheetsFindReplacePreset,
  UniverPresetSheetsFindReplaceZhCN,
  UniverSheetsConditionalFormattingPreset,
  UniverPresetSheetsConditionalFormattingZhCN,
};

export default secondaryDeps;
