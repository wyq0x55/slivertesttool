/*
 * Full-text hover bubble for truncated cells (design: mimic the filter panel's
 * long-option tooltip, but for normal cells).
 *
 * Univer renders cell text on a <canvas>, so an overflowed value that is visually
 * clipped has no native browser tooltip. This module reproduces the behaviour the
 * user likes from the column-filter dropdown:
 *
 *   1) WHICH cell + WHEN — reuse Univer's own `HoverManagerService.currentCell$`
 *      (the same service that powers data-validation / hyperlink cell popups), so
 *      we get the hovered {unitId, row, col} with Univer's built-in debouncing and
 *      correct hit-testing across frozen panes / scroll / zoom.
 *   2) IS IT TRUNCATED — measure the cell's display text with a canvas 2D context
 *      whose font matches Univer's (`<size>pt <family>`, same unit Univer uses in
 *      getFontStyleString) and compare to the column width, accounting for text
 *      that legitimately spills into consecutive EMPTY right-hand neighbours
 *      (Univer's overflow rendering — those are NOT clipped, so no bubble).
 *   3) SHOW — a plain absolutely-positioned <div> bubble in the host, located via
 *      the same affine-matrix projection the remote-cursor overlay uses
 *      (verified == Viewport.getAbsoluteVector). Shown after a short hover delay
 *      and hidden on cell change / mouse-leave / scroll / edit.
 *
 * Scope (per current requirements): single-line, single (non-merged) cells with
 * plain text. Wrapped cells, merged cells and rich text are intentionally skipped.
 */
import { projectCell } from "./collab_overlay";

export interface CellTooltipDeps {
  /** The Univer instance (from `createUniver({...}).univer`). */
  getUniver: () => any | null;
  /** Active worksheet unitId (workbook unitId). */
  getUnitId: () => string | null;
  /** Active FWorksheet (facade) for reading cell text / style / column width. */
  getFSheet: () => any | null;
  /** When true, suppress the bubble (e.g. while a cell editor is open). */
  isSuppressed?: () => boolean;
  /**
   * DI identifiers injected from main.ts (a UMD/IIFE bundle never exposes these
   * on a global): the render manager + skeleton for projection, and the hover
   * service for hover detection.
   */
  identifiers?: {
    IRenderManagerService?: any;
    SheetSkeletonManagerService?: any;
    HoverManagerService?: any;
  };
}

interface Rect { left: number; top: number; width: number; height: number; }

const BUBBLE_CLASS = "lm-cell-tooltip";
const SHOW_DELAY_MS = 350;
// Univer's default cell text padding (left+right) in px; subtracted from the
// column width to get the usable text area. Slightly generous so borderline
// values do not flicker a bubble.
const CELL_PADDING_PX = 6;
// Cap how many empty right-hand neighbours we sum when deciding whether an
// overflowing value is actually fully visible.
const MAX_OVERFLOW_SCAN = 40;

export class CellTooltip {
  private host: HTMLElement;
  private deps: CellTooltipDeps;
  private bubble: HTMLDivElement;
  private measurer: CanvasRenderingContext2D | null;
  private sub: { unsubscribe?: () => void } | null = null;
  private showTimer: number = 0;
  private disposed = false;
  private curKey: string | null = null;

  constructor(host: HTMLElement, deps: CellTooltipDeps) {
    this.host = host;
    this.deps = deps;

    const cs = getComputedStyle(host);
    if (cs.position === "static") host.style.position = "relative";

    this.bubble = document.createElement("div");
    this.bubble.className = BUBBLE_CLASS;
    Object.assign(this.bubble.style, {
      position: "absolute",
      display: "none",
      maxWidth: "420px",
      padding: "4px 8px",
      borderRadius: "4px",
      background: "rgba(32,33,36,0.95)",
      color: "#fff",
      font: "12px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif",
      whiteSpace: "pre-wrap",
      wordBreak: "break-word",
      boxShadow: "0 2px 8px rgba(0,0,0,0.25)",
      pointerEvents: "none",
      zIndex: "30", // above the remote-cursor overlay (z-index 20)
    } as CSSStyleDeclaration);
    host.appendChild(this.bubble);

    try {
      this.measurer = document.createElement("canvas").getContext("2d");
    } catch (_e) {
      this.measurer = null;
    }

    // Hide immediately on scroll/wheel so a stale bubble never lingers at the
    // old position; the next hover re-evaluates.
    this.onWheel = this.onWheel.bind(this);
    host.addEventListener("wheel", this.onWheel, { passive: true });
  }

  /** Subscribe to hover events. Safe to call repeatedly (idempotent). */
  init(): void {
    if (this.disposed || this.sub) return;
    const hover = this.resolveHoverService();
    const stream = hover && hover.currentCell$;
    if (!stream || typeof stream.subscribe !== "function") return;
    this.sub = stream.subscribe((pos: any) => this.onHover(pos));
  }

  dispose(): void {
    this.disposed = true;
    if (this.showTimer) { window.clearTimeout(this.showTimer); this.showTimer = 0; }
    try { this.sub && this.sub.unsubscribe && this.sub.unsubscribe(); } catch (_e) { /* noop */ }
    this.sub = null;
    this.host.removeEventListener("wheel", this.onWheel);
    try { this.bubble.remove(); } catch (_e) { /* noop */ }
  }

  // ---------------------------------------------------------------- internals --

  private onWheel(): void {
    this.hide();
  }

  private onHover(pos: any): void {
    if (this.disposed) return;
    if (this.showTimer) { window.clearTimeout(this.showTimer); this.showTimer = 0; }

    const loc = pos && pos.location;
    if (!loc || typeof loc.row !== "number" || typeof loc.col !== "number") {
      this.hide();
      return;
    }
    if (this.deps.isSuppressed && this.deps.isSuppressed()) { this.hide(); return; }

    const key = `${loc.row}:${loc.col}`;
    // Same cell as currently shown -> nothing to do (avoid re-measuring on every
    // intra-cell mousemove).
    if (key === this.curKey && this.bubble.style.display !== "none") return;
    this.curKey = key;
    this.hide();

    const text = this.truncatedText(loc.row, loc.col);
    if (text == null) return; // fits, empty, wrapped, or unreadable -> no bubble

    // Classic tooltip feel: only pop after the pointer rests briefly.
    this.showTimer = window.setTimeout(() => {
      this.showTimer = 0;
      if (this.disposed) return;
      this.show(loc.row, loc.col, text);
    }, SHOW_DELAY_MS);
  }

  /**
   * Returns the full cell text when it is horizontally clipped, or null when it
   * fits, is empty, is wrapped, or cannot be read/measured.
   */
  private truncatedText(row: number, col: number): string | null {
    const fsheet = this.deps.getFSheet && this.deps.getFSheet();
    if (!fsheet || !this.measurer) return null;
    let range: any;
    try { range = fsheet.getRange(row, col); } catch (_e) { return null; }
    if (!range) return null;

    // Read rich-text aware so a multi-line cell (stored as cell.p) yields its full
    // text WITH newlines, not a flattened single line. getValue(true) returns a
    // RichTextValue for such cells (has toPlainText()); otherwise a primitive.
    let text = "";
    try {
      let dv: any = null;
      if (range.getValue) { try { dv = range.getValue(true); } catch (_e2) { dv = null; } }
      if (dv != null && typeof dv.toPlainText === "function") {
        text = String(dv.toPlainText()).replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\n$/, "");
      } else {
        const plain = range.getDisplayValue ? range.getDisplayValue() : range.getValue();
        text = plain == null ? "" : String(plain);
      }
    } catch (_e) { return null; }
    if (!text) return null;

    // Multi-line cell: with uniform row height (no wrap) only the first line is
    // visible in the grid, so the full text is always "clipped" — show the bubble
    // with every line (the bubble is pre-wrap). This is the primary reveal path
    // for newline cells now that they are stored as rich text.
    if (text.indexOf("\n") !== -1) return text;

    // Skip genuinely wrapped cells (out of scope): those grow the row instead of
    // clipping. We never enable wrap ourselves, so this is normally a no-op.
    try { if (range.getWrap && range.getWrap()) return null; } catch (_e) { /* treat as no-wrap */ }

    // Build a measuring font matching Univer's getFontStyleString: "<it> <bl>
    // <ceil(fs)>pt <ff>". pt (not px) is deliberate — canvas converts it the same
    // way Univer's own measuring context does, so widths compare directly to the
    // px column width.
    let fs = 11, ff = "Arial", bold = false, italic = false;
    try { const v = range.getFontSize && range.getFontSize(); if (v) fs = v; } catch (_e) { /* default */ }
    try { const v = range.getFontFamily && range.getFontFamily(); if (v) ff = v; } catch (_e) { /* default */ }
    try {
      const st = range.getCellStyle && range.getCellStyle();
      if (st) { bold = !!st.bl; italic = !!st.it; }
    } catch (_e) { /* default weight/style */ }
    this.measurer.font =
      `${italic ? "italic " : ""}${bold ? "bold " : ""}${Math.ceil(fs)}pt ${ff}`;
    const textW = this.measurer.measureText(text).width;

    let colW = 0;
    try { colW = fsheet.getColumnWidth(col) || 0; } catch (_e) { return null; }
    const avail = colW - CELL_PADDING_PX;
    if (textW <= avail) return null; // fits in its own column

    // Univer spills left-/general-aligned overflow into consecutive EMPTY right
    // neighbours; such text is fully visible and must NOT get a bubble. Sum their
    // widths until the text fits or a non-empty cell blocks the overflow.
    let room = avail;
    for (let c = col + 1; c < col + 1 + MAX_OVERFLOW_SCAN && room < textW; c++) {
      let v: any = "";
      try { v = fsheet.getRange(row, c).getValue(); } catch (_e) { v = ""; }
      if (v !== "" && v != null) break; // blocked -> remaining text is clipped
      let w = 0;
      try { w = fsheet.getColumnWidth(c) || 0; } catch (_e) { w = 0; }
      room += w;
    }
    if (room >= textW) return null; // fully visible via overflow

    return text; // clipped -> show the full text
  }

  private show(row: number, col: number, text: string): void {
    const rect = this.cellHostRect(row, col);
    if (!rect) return;

    this.bubble.textContent = text;
    this.bubble.style.display = "block";

    // Measure the bubble to keep it inside the host; prefer below the cell, flip
    // above when there is not enough room underneath.
    const bw = this.bubble.offsetWidth;
    const bh = this.bubble.offsetHeight;
    const hw = this.host.clientWidth;
    const hh = this.host.clientHeight;

    let left = rect.left;
    if (left + bw > hw) left = Math.max(0, hw - bw);
    if (left < 0) left = 0;

    let top = rect.top + rect.height + 2;
    if (top + bh > hh) {
      const above = rect.top - bh - 2;
      top = above >= 0 ? above : Math.max(0, hh - bh);
    }

    this.bubble.style.left = `${Math.round(left)}px`;
    this.bubble.style.top = `${Math.round(top)}px`;
  }

  private hide(): void {
    if (this.bubble.style.display !== "none") this.bubble.style.display = "none";
  }

  // -- projection (same affine-matrix transform as the remote-cursor overlay) --

  private cellHostRect(row: number, col: number): Rect | null {
    const univer = this.deps.getUniver && this.deps.getUniver();
    const unitId = this.deps.getUnitId && this.deps.getUnitId();
    if (!univer || !unitId || typeof univer.__getInjector !== "function") return null;
    let injector: any;
    try { injector = univer.__getInjector(); } catch (_e) { return null; }
    if (!injector) return null;

    const ids = this.deps.identifiers || {};
    const rms = this.get(injector, ids.IRenderManagerService, "IRenderManagerService");
    const render = rms && typeof rms.getRenderById === "function"
      ? rms.getRenderById(unitId) : null;
    if (!render) return null;

    let skm: any = null;
    const skmId = ids.SheetSkeletonManagerService;
    try { if (render.with && skmId) skm = render.with(skmId); } catch (_e) { /* fall through */ }
    if (!skm) skm = this.get(injector, skmId, "SheetSkeletonManagerService");
    const skeleton = skm && typeof skm.getCurrentSkeleton === "function"
      ? skm.getCurrentSkeleton() : null;
    if (!skeleton || typeof skeleton.getCellWithCoordByIndex !== "function") return null;

    let cell: any;
    try { cell = skeleton.getCellWithCoordByIndex(row, col); } catch (_e) { return null; }
    if (!cell) return null;

    const scene = render.scene;
    if (!scene) return null;
    let m: number[];
    if (scene.transform && typeof scene.transform.getMatrix === "function") {
      m = scene.transform.getMatrix();
    } else {
      const s = (scene.scaleX || (scene.getScale && scene.getScale().x)) || 1;
      m = [s, 0, 0, s, 0, 0];
    }

    const viewport = this.pickViewport(scene);
    let scroll = { x: 0, y: 0 };
    if (viewport) {
      try {
        if (typeof viewport.getViewportScrollByScrollXY === "function") {
          scroll = viewport.getViewportScrollByScrollXY() || scroll;
        } else {
          scroll = { x: viewport.viewportScrollX || 0, y: viewport.viewportScrollY || 0 };
        }
      } catch (_e) { /* keep zero */ }
    }

    const r = projectCell(cell, m, scroll.x, scroll.y);

    // Affine result is canvas-relative; convert to host-relative.
    let offX = 0, offY = 0;
    const canvas: HTMLElement | null =
      (render.engine && render.engine.getCanvasElement && render.engine.getCanvasElement()) ||
      this.host.querySelector("canvas");
    if (canvas) {
      try {
        const cr = canvas.getBoundingClientRect();
        const hr = this.host.getBoundingClientRect();
        offX = cr.left - hr.left;
        offY = cr.top - hr.top;
      } catch (_e) { /* assume aligned */ }
    }
    return { left: r.left + offX, top: r.top + offY, width: r.width, height: r.height };
  }

  private pickViewport(scene: any): any {
    if (!scene) return null;
    const keys = ["viewMain", "VIEW_MAIN", "sheetViewMain"];
    for (const k of keys) {
      try { const vp = scene.getViewport && scene.getViewport(k); if (vp) return vp; }
      catch (_e) { /* try next */ }
    }
    try {
      const all = scene.getViewports && scene.getViewports();
      if (Array.isArray(all)) {
        return all.find((v: any) => v && (v.viewportScrollX || v.viewportScrollY)) || all[0];
      }
    } catch (_e) { /* noop */ }
    return null;
  }

  private resolveHoverService(): any {
    const univer = this.deps.getUniver && this.deps.getUniver();
    if (!univer || typeof univer.__getInjector !== "function") return null;
    let injector: any;
    try { injector = univer.__getInjector(); } catch (_e) { return null; }
    if (!injector) return null;
    return this.get(injector, (this.deps.identifiers || {}).HoverManagerService, "HoverManagerService");
  }

  private get(injector: any, preferred: any, name: string): any {
    if (preferred) { try { const s = injector.get(preferred); if (s) return s; } catch (_e) { /* fall through */ } }
    const g: any = (globalThis as any).UniverCore || (globalThis as any).Univer || globalThis;
    const id = g && g[name];
    if (id) { try { const s = injector.get(id); if (s) return s; } catch (_e) { /* noop */ } }
    return null;
  }
}
