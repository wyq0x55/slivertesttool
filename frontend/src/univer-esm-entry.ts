import "./univer-globals";
import {
  createUniver,
  defaultTheme,
  LocaleService,
  LocaleType,
  mergeLocales,
} from "@univerjs/presets";
import { UniverSheetsCorePreset } from "@univerjs/preset-sheets-core";
import UniverPresetSheetsCoreZhCN from "@univerjs/preset-sheets-core/locales/zh-CN";

import { UniverSheetsFilterPreset } from "@univerjs/preset-sheets-filter";
import UniverPresetSheetsFilterZhCN from "@univerjs/preset-sheets-filter/locales/zh-CN";

import { UniverSheetsSortPreset } from "@univerjs/preset-sheets-sort";
import UniverPresetSheetsSortZhCN from "@univerjs/preset-sheets-sort/locales/zh-CN";

import { UniverSheetsTablePreset } from "@univerjs/preset-sheets-table";
import UniverPresetSheetsTableZhCN from "@univerjs/preset-sheets-table/locales/zh-CN";

import { UniverSheetsCrosshairHighlightPlugin } from "@univerjs/sheets-crosshair-highlight";
import SheetsCrosshairHighlightZhCN from "@univerjs/sheets-crosshair-highlight/locale/zh-CN";
import "@univerjs/sheets-crosshair-highlight/facade";

import { IRenderManagerService } from "@univerjs/engine-render";
import { HoverManagerService, SheetSkeletonManagerService } from "@univerjs/sheets-ui";

import { MountOpts, UniverDeps, UniverGridAdapter } from "./adapter";
import { createStepsMount, UniverStepsView } from "./steps_adapter";

declare global {
  interface Window {
    LMUniver?: { mount(opts: MountOpts): UniverGridAdapter };
    LMUniverSteps?: { mount(host: HTMLElement, opts?: any): UniverStepsView };
  }
}

const deps: UniverDeps = {
  createUniver,
  defaultTheme,
  LocaleType,
  LocaleService,
  mergeLocales,
  UniverSheetsCorePreset,
  UniverPresetSheetsCoreZhCN,
  UniverSheetsFilterPreset,
  UniverPresetSheetsFilterZhCN,
  UniverSheetsSortPreset,
  UniverPresetSheetsSortZhCN,
  UniverSheetsTablePreset,
  UniverPresetSheetsTableZhCN,
  UniverSheetsCrosshairHighlightPlugin,
  SheetsCrosshairHighlightZhCN,
  IRenderManagerService,
  SheetSkeletonManagerService,
  HoverManagerService,
};

const mountedAdapters = new Set<UniverGridAdapter>();
const mountedStepsViews = new Set<UniverStepsView>();
let secondaryPromise: Promise<Partial<UniverDeps>> | null = null;
let secondaryScheduled = false;

function loadSecondaryPresets(): Promise<Partial<UniverDeps>> {
  if (!secondaryPromise) {
    secondaryPromise = import("./univer-secondary")
      .then((mod) => {
        const extra = mod.secondaryDeps || mod.default || {};
        Object.assign(deps, extra);
        mountedAdapters.forEach((adapter) => {
          adapter.attachSecondaryPresets(extra);
        });
        mountedStepsViews.forEach((view) => {
          view.attachSecondaryPresets(extra);
        });
        return extra;
      })
      .catch((err) => {
        console.warn("[LMUniver] secondary presets load failed:", err);
        return {};
      });
  }
  return secondaryPromise;
}

function scheduleSecondaryLoad(): void {
  if (secondaryScheduled) return;
  secondaryScheduled = true;
  const ric = (window as any).requestIdleCallback;
  if (typeof ric === "function") {
    ric(() => {
      void loadSecondaryPresets();
    });
  } else {
    window.setTimeout(() => {
      void loadSecondaryPresets();
    }, 120);
  }
}

window.LMUniver = {
  mount(opts: MountOpts): UniverGridAdapter {
    const adapter = new UniverGridAdapter(deps, opts);
    let isDisposed = false;
    (adapter as any)._onDisposed = () => {
      isDisposed = true;
      mountedAdapters.delete(adapter);
    };
    adapter.init();
    if (!isDisposed) mountedAdapters.add(adapter);
    scheduleSecondaryLoad();
    return adapter;
  },
};
(window.LMUniver as any).loadSecondary = loadSecondaryPresets;

const baseStepsMount = createStepsMount(deps);
window.LMUniverSteps = {
  mount(host: HTMLElement, opts?: any): UniverStepsView {
    const view = baseStepsMount(host, opts);
    (view as any)._onDisposed = () => {
      mountedStepsViews.delete(view);
    };
    mountedStepsViews.add(view);
    scheduleSecondaryLoad();
    return view;
  },
};
