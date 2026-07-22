/**
 * collab_overlay.ts — Collaborative selection overlay for the Univer sheet grid.
 *
 * Renders every remote peer's selection as a COLOURED BORDER on a transparent DOM
 * layer floating above the Univer canvas (Figma / Google-Sheets style):
 *
 *   - anchor cell  -> a thick 2px border box + a name label at its top-right
 *   - selection    -> a 1.5px translucent border around each contiguous run of
 *                     selected rows within the peer's visible column span
 *
 * Design goals (per review notes):
 *   - ZERO document mutation. No setBorder / setBackgroundColor. The document
 *     model is never touched, so there is nothing to "restore" on clear and no
 *     way to pollute cell styles.
 *   - VIEW-INDEPENDENT selection. The caller passes SHEET ROW indices it already
 *     resolved from the peer's broadcast row-uuid set (see INTEGRATION.md §2),
 *     so a peer's differently sorted/filtered view can never mis-place a box.
 *   - RESILIENT coordinate access. Univer 0.25.1 exposes the skeleton + viewport
 *     through DI identifiers that a UMD/preset bundle may or may not re-export;
 *     every service lookup is probed defensively and the overlay degrades to a
 *     no-op (never throws) when a hook is missing.
 *
 * Verified against the Univer 0.25.1 type definitions shipped with this project:
 *   - univer.__getInjector(): Injector                         (core/univer.d.ts)
 *   - IRenderManagerService.getRenderById(unitId): IRender     (engine-render)
 *   - SheetSkeletonManagerService.getCurrentSkeleton()         (sheets-ui)
 *   - SpreadsheetSkeleton.getCellWithCoordByIndex(row,col,header?): ICellWithCoord
 *       -> { startX, startY, endX, endY }                      (core/typedef.d.ts)
 *   - Viewport.viewportScrollX / viewportScrollY               (engine-render)
 *   - Scene.scaleX / scaleY
 *
 * The ONE thing that must be confirmed in the built browser env is the exact
 * content->CSS pixel transform (headers + freeze + scroll + scale). It is fully
 * isolated in `projectCell()`; run `probeProjection()` once in devtools to lock
 * it down. See INTEGRATION.md §5.
 */

// ---------------------------------------------------------------------------
// Public data shapes (all coordinates are SHEET indices, header rows included).
// ---------------------------------------------------------------------------

export interface OverlaySelection {
  /** Stable per-peer key (e.g. the row-uuid owner's client/user id as string). */
  key: string;
  /** Peer display name (shown in the anchor label). */
  name: string;
  /** Peer colour, any CSS colour string (e.g. "#e6194b"). */
  color: string;
  /** Active cell in sheet coords, or null to skip the anchor box + label. */
  anchor: { row: number; col: number } | null;
  /**
   * Selected cells as SHEET (row,col) pairs. The overlay groups them into
   * contiguous per-row rectangles for drawing; passing a sparse set is fine.
   */
  cells: Array<{ row: number; col: number }>;
}

/** Minimal structural type for the value returned by `univer.__getInjector()`. */
interface InjectorLike {
  get(id: any): any;
  has?(id: any): boolean;
}

export interface CollabOverlayDeps {
  /** The Univer instance (from `createUniver({...}).univer`). */
  getUniver: () => any | null;
  /** Active worksheet unitId (workbook unitId). */
  getUnitId: () => string | null;
  /**
   * The DI identifiers, if the host bundle can provide them. When omitted the
   * overlay falls back to name-based probing of the injector. Pass whatever the
   * preset re-exports (often available on the global Univer namespace).
   */
  identifiers?: {
    IRenderManagerService?: any;
    SheetSkeletonManagerService?: any;
  };
}

// ---------------------------------------------------------------------------

const LAYER_CLASS = "lm-collab-overlay";
const ANCHOR_BORDER_PX = 2;
const RANGE_BORDER_PX = 1.5;

export class CollabOverlay {
  private deps: CollabOverlayDeps;
  private host: HTMLElement;
  private layer: HTMLDivElement;
  private selections: OverlaySelection[] = [];
  private ro: ResizeObserver | null = null;
  private rafId = 0;
  private disposed = false;
  private scrollUnsub: (() => void) | null = null;

  /**
   * @param host  The Univer host element (adapter's `this.host`); the overlay
   *              layer is absolutely positioned inside it, so `host` must be a
   *              positioned element (the constructor enforces `position`).
   */
  constructor(host: HTMLElement, deps: CollabOverlayDeps) {
    this.host = host;
    this.deps = deps;

    const cs = getComputedStyle(host);
    if (cs.position === "static") host.style.position = "relative";

    this.layer = document.createElement("div");
    this.layer.className = LAYER_CLASS;
    Object.assign(this.layer.style, {
      position: "absolute",
      inset: "0",
      overflow: "hidden",
      pointerEvents: "none", // never steal grid clicks
      zIndex: "20",
    } as CSSStyleDeclaration);
    host.appendChild(this.layer);

    // Reproject on container resize.
    if (typeof ResizeObserver !== "undefined") {
      this.ro = new ResizeObserver(() => this.scheduleRefresh());
      this.ro.observe(host);
    }
    // Reproject on scroll/zoom: the Univer canvas fires wheel + our own hook.
    this.attachScrollHook();
  }

  /** Replace the current set of remote selections and repaint. */
  setSelections(list: OverlaySelection[]): void {
    this.selections = Array.isArray(list) ? list : [];
    this.scheduleRefresh();
  }

  /** Force a reprojection (call after the sheet re-renders or tabs switch). */
  refresh(): void {
    this.scheduleRefresh();
  }

  dispose(): void {
    this.disposed = true;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    if (this.ro) { try { this.ro.disconnect(); } catch (_e) { /* noop */ } }
    if (this.scrollUnsub) { try { this.scrollUnsub(); } catch (_e) { /* noop */ } }
    try { this.layer.remove(); } catch (_e) { /* noop */ }
  }

  // ---- rendering --------------------------------------------------------- //

  private scheduleRefresh(): void {
    if (this.disposed || this.rafId) return;
    this.rafId = requestAnimationFrame(() => {
      this.rafId = 0;
      try { this.paint(); } catch (_e) { /* overlay is best-effort */ }
    });
  }

  private paint(): void {
    // Clear then rebuild — selection sets are small (peers × visible rows), so a
    // full rebuild per frame is cheaper and simpler than diffing DOM nodes.
    this.layer.replaceChildren();

    const proj = this.getProjector();
    if (!proj) return; // skeleton not ready yet; a later refresh will retry

    // Clip the overlay to the data viewport when we know it: the layer is
    // sized/positioned to the main viewport rect with overflow:hidden, so any
    // cursor scrolled up/left under the frozen headers — or past a viewport edge —
    // is clipped instead of floating over the headers or outside the grid. Box
    // coordinates are then made RELATIVE to the clip origin. When the viewport
    // rect is unknown (clip === null) we fall back to covering the whole host and
    // draw without clipping so cursors always show.
    const clip = proj.clip;
    if (clip) {
      Object.assign(this.layer.style, {
        left: `${Math.round(clip.left)}px`,
        top: `${Math.round(clip.top)}px`,
        width: `${Math.max(0, Math.round(clip.width))}px`,
        height: `${Math.max(0, Math.round(clip.height))}px`,
        right: "auto",
        bottom: "auto",
        inset: "auto",
      } as CSSStyleDeclaration);
    } else {
      Object.assign(this.layer.style, {
        left: "0px",
        top: "0px",
        right: "0px",
        bottom: "0px",
        width: "auto",
        height: "auto",
      } as CSSStyleDeclaration);
    }
    const ox = clip ? clip.left : 0;
    const oy = clip ? clip.top : 0;
    const rel = (r: Rect): Rect => ({
      left: r.left - ox,
      top: r.top - oy,
      width: r.width,
      height: r.height,
    });

    for (const sel of this.selections) {
      // 1) Selection outline: draw ONE box around the whole selection (its
      //    bounding range) instead of a box per row. For a rectangular range —
      //    the usual case — this is exactly the single large frame collaborators
      //    expect; sparse selections still get a clean enclosing rectangle.
      const span = cellSpan(sel.cells);
      if (span) {
        const a = proj.rectAt(span.minRow, span.minCol);
        const b = proj.rectAt(span.maxRow, span.maxCol);
        if (a && b) {
          const rect = rel(boundingRect(a, b));
          this.layer.appendChild(
            borderBox(rect, sel.color, RANGE_BORDER_PX, 0.85),
          );
        }
      }
      // 2) Anchor box (thicker) + name label.
      if (sel.anchor) {
        const c = proj.rectAt(sel.anchor.row, sel.anchor.col);
        if (c) {
          const box = rel(boundingRect(c, c));
          this.layer.appendChild(borderBox(box, sel.color, ANCHOR_BORDER_PX, 1));
          this.layer.appendChild(nameLabel(box, sel.name, sel.color));
        }
      }
    }
  }

  // ---- Univer service access (defensive) --------------------------------- //

  private getInjector(): InjectorLike | null {
    const univer = this.deps.getUniver && this.deps.getUniver();
    if (!univer || typeof univer.__getInjector !== "function") return null;
    try { return univer.__getInjector() as InjectorLike; }
    catch (_e) { return null; }
  }

  private resolve(injector: InjectorLike, preferred: any, names: string[]): any {
    // Try the caller-supplied DI identifier first, then probe by known names on
    // the global Univer namespace (UMD builds expose them there).
    if (preferred) { try { return injector.get(preferred); } catch (_e) { /* fall through */ } }
    const g: any = (globalThis as any).UniverCore || (globalThis as any).Univer || globalThis;
    for (const n of names) {
      const id = g && g[n];
      if (!id) continue;
      try { const s = injector.get(id); if (s) return s; } catch (_e) { /* try next */ }
    }
    return null;
  }

  /**
   * Build the projection needed to paint the overlay, or null when the skeleton /
   * viewport is not ready yet. Returns:
   *   - `rectAt(row,col)` -> a cell's on-screen rect in HOST-relative CSS pixels,
   *   - `clip`            -> the main viewport rectangle (also HOST-relative), so
   *                          the caller can clip cursors that scrolled out of the
   *                          data area (under the frozen headers or past an edge).
   *
   * The scene->screen transform is taken straight from Univer's own affine
   * matrix (Scene.transform) and scroll (Viewport.getViewportScrollByScrollXY),
   * i.e. the exact math of Viewport.getAbsoluteVector:
   *     screen = transform.applyPoint(sceneCoord - scroll)
   * so header offset + freeze + scroll + zoom are ALL handled by Univer, never
   * hand-rolled. This is why a peer resizing a column / row, scrolling, or
   * zooming keeps the box glued to the right cell: every paint re-reads the live
   * skeleton coords and the live matrix.
   */
  private getProjector(): {
    rectAt: (row: number, col: number) => Rect | null;
    clip: Rect | null;
  } | null {
    const injector = this.getInjector();
    const unitId = this.deps.getUnitId && this.deps.getUnitId();
    if (!injector || !unitId) return null;

    const ids = this.deps.identifiers || {};
    const rms = this.resolve(injector, ids.IRenderManagerService, [
      "IRenderManagerService",
    ]);
    const render = rms && typeof rms.getRenderById === "function"
      ? rms.getRenderById(unitId)
      : null;
    if (!render) return null;

    // The skeleton manager is registered per-render; try render.with() first
    // (0.25.1 IRender exposes a scoped injector), then the root injector.
    let skm: any = null;
    const skmId = ids.SheetSkeletonManagerService;
    try {
      if (render.with && skmId) skm = render.with(skmId);
    } catch (_e) { /* fall through */ }
    if (!skm) {
      skm = this.resolve(injector, skmId, ["SheetSkeletonManagerService"]);
    }
    const skeleton = skm && typeof skm.getCurrentSkeleton === "function"
      ? skm.getCurrentSkeleton()
      : null;
    // Univer 0.25.1 SpreadsheetSkeleton exposes getCellWithCoordByIndex(row,col,
    // header?) -> ICellWithCoord {startX,startY,endX,endY}. header defaults to
    // true, so the returned coords already include the header offset, matching
    // the scene coordinate space the transform below expects.
    if (!skeleton || typeof skeleton.getCellWithCoordByIndex !== "function") return null;

    const scene = render.scene;
    if (!scene) return null;
    const viewport = pickMainViewport(scene);

    // Scene->canvas affine matrix [a,b,c,d,e,f]: screenX = a*x + c*y + e;
    // screenY = b*x + d*y + f. Prefer Univer's own matrix (it folds in zoom,
    // device-pixel-ratio and any translation); if it isn't available fall back to
    // a plain scale so we still DRAW (possibly with a small offset) rather than
    // silently drawing nothing — a missing viewport/transform must never blank
    // out remote cursors.
    let m: number[];
    if (scene.transform && typeof scene.transform.getMatrix === "function") {
      m = scene.transform.getMatrix();
    } else {
      const s = (scene.scaleX || (scene.getScale && scene.getScale().x)) || 1;
      m = [s, 0, 0, s, 0, 0];
    }
    // Scroll in SCENE units (same space as the cell coords), subtracted BEFORE the
    // matrix — exactly what getAbsoluteVector does. No viewport -> no scroll (0,0).
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

    // The affine transform yields CANVAS-relative pixels; convert to HOST-relative
    // by adding the canvas' offset within the host (usually 0, but never assume).
    const canvas: HTMLElement | null =
      (render.engine && render.engine.getCanvasElement && render.engine.getCanvasElement()) ||
      this.host.querySelector("canvas");
    let offX = 0, offY = 0;
    if (canvas) {
      try {
        const cr = canvas.getBoundingClientRect();
        const hr = this.host.getBoundingClientRect();
        offX = cr.left - hr.left;
        offY = cr.top - hr.top;
      } catch (_e) { /* assume aligned */ }
    }

    const toScreen = (sx: number, sy: number): { x: number; y: number } => {
      const x = sx - scroll.x;
      const y = sy - scroll.y;
      return { x: m[0] * x + m[2] * y + m[4] + offX, y: m[1] * x + m[3] * y + m[5] + offY };
    };

    // Main viewport rect in HOST coords: the DATA area only (excludes the frozen
    // row/column headers). Used to clip cursors scrolled out of view. Only trust
    // it when we have a viewport with a positive size; otherwise leave it null so
    // paint() covers the whole host and skips clipping (never blanks the overlay).
    let clip: Rect | null = null;
    if (viewport) {
      const vpW = (viewport.width as number) || 0;
      const vpH = (viewport.height as number) || 0;
      if (vpW > 0 && vpH > 0) {
        clip = {
          left: ((viewport.left as number) || 0) + offX,
          top: ((viewport.top as number) || 0) + offY,
          width: vpW,
          height: vpH,
        };
      }
    }

    const rectAt = (row: number, col: number): Rect | null => {
      let cell: any;
      try { cell = skeleton.getCellWithCoordByIndex(row, col); }
      catch (_e) { return null; }
      if (!cell) return null;
      const a = toScreen(cell.startX, cell.startY);
      const b = toScreen(cell.endX, cell.endY);
      return {
        left: Math.min(a.x, b.x),
        top: Math.min(a.y, b.y),
        width: Math.abs(b.x - a.x),
        height: Math.abs(b.y - a.y),
      };
    };

    return { rectAt, clip };
  }

  private attachScrollHook(): void {
    // Univer repaints on its own canvas; the cheapest cross-version signal is a
    // wheel/pointer listener on the host plus a periodic reconcile while any
    // remote selection is present. Both just schedule a reproject.
    const onEvt = () => this.scheduleRefresh();
    this.host.addEventListener("wheel", onEvt, { passive: true });
    this.host.addEventListener("pointerup", onEvt, { passive: true });
    // Low-frequency safety net for programmatic scrolls (keyboard nav, etc.).
    const timer = window.setInterval(() => {
      if (this.selections.length) this.scheduleRefresh();
    }, 250);
    this.scrollUnsub = () => {
      this.host.removeEventListener("wheel", onEvt);
      this.host.removeEventListener("pointerup", onEvt);
      window.clearInterval(timer);
    };
  }
}

// ---------------------------------------------------------------------------
// Pure geometry helpers.
// ---------------------------------------------------------------------------

interface Rect { left: number; top: number; width: number; height: number; }

/**
 * Scene-space cell rect -> canvas CSS pixels, using Univer's own affine matrix
 * and scroll — the exact math of Viewport.getAbsoluteVector:
 *     screen = transform.applyPoint(sceneCoord - scroll)
 * with `m = scene.transform.getMatrix()` = [a,b,c,d,e,f]:
 *     screenX = a*x + c*y + e ;  screenY = b*x + d*y + f ;  (x,y) = coord - scroll
 * Header offset, freeze, scroll and zoom are all baked into (m, scroll), so no
 * value is ever hardcoded and a resize/scroll/zoom just changes the inputs.
 */
export function projectCell(
  cell: { startX: number; startY: number; endX: number; endY: number },
  m: number[],
  scrollX: number,
  scrollY: number,
): Rect {
  const at = (sx: number, sy: number) => {
    const x = sx - scrollX;
    const y = sy - scrollY;
    return { x: m[0] * x + m[2] * y + m[4], y: m[1] * x + m[3] * y + m[5] };
  };
  const a = at(cell.startX, cell.startY);
  const b = at(cell.endX, cell.endY);
  return {
    left: Math.min(a.x, b.x),
    top: Math.min(a.y, b.y),
    width: Math.abs(b.x - a.x),
    height: Math.abs(b.y - a.y),
  };
}

function boundingRect(a: Rect, b: Rect): Rect {
  const left = Math.min(a.left, b.left);
  const top = Math.min(a.top, b.top);
  const right = Math.max(a.left + a.width, b.left + b.width);
  const bottom = Math.max(a.top + a.height, b.top + b.height);
  return { left, top, width: right - left, height: bottom - top };
}

/**
 * Bounding range of a set of (row,col) cells -> {minRow,minCol,maxRow,maxCol},
 * or null when empty. Used to draw the selection as ONE enclosing rectangle.
 */
export function cellSpan(
  cells: Array<{ row: number; col: number }>,
): { minRow: number; minCol: number; maxRow: number; maxCol: number } | null {
  if (!cells || cells.length === 0) return null;
  let minRow = Infinity, minCol = Infinity, maxRow = -Infinity, maxCol = -Infinity;
  for (const { row, col } of cells) {
    if (row < minRow) minRow = row;
    if (row > maxRow) maxRow = row;
    if (col < minCol) minCol = col;
    if (col > maxCol) maxCol = col;
  }
  return { minRow, minCol, maxRow, maxCol };
}

function borderBox(r: Rect, color: string, borderPx: number, alpha: number): HTMLDivElement {
  const el = document.createElement("div");
  Object.assign(el.style, {
    position: "absolute",
    left: `${Math.round(r.left)}px`,
    top: `${Math.round(r.top)}px`,
    width: `${Math.max(0, Math.round(r.width) - borderPx)}px`,
    height: `${Math.max(0, Math.round(r.height) - borderPx)}px`,
    border: `${borderPx}px solid ${color}`,
    boxSizing: "border-box",
    borderRadius: "1px",
    opacity: String(alpha),
    pointerEvents: "none",
  } as CSSStyleDeclaration);
  return el;
}

function nameLabel(anchor: Rect, name: string, color: string): HTMLDivElement {
  const el = document.createElement("div");
  el.textContent = name || "Collaborator";
  Object.assign(el.style, {
    position: "absolute",
    left: `${Math.round(anchor.left)}px`,
    // Sit just above the anchor's top edge, right-aligned to its left corner.
    top: `${Math.round(anchor.top) - 16}px`,
    maxWidth: "160px",
    padding: "0 5px",
    height: "15px",
    lineHeight: "15px",
    fontSize: "11px",
    fontFamily: "system-ui, sans-serif",
    color: "#fff",
    background: color,
    borderRadius: "3px 3px 3px 0",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
    boxShadow: "0 1px 2px rgba(0,0,0,.25)",
    pointerEvents: "none",
    zIndex: "1",
  } as CSSStyleDeclaration);
  return el;
}

function pickMainViewport(scene: any): any {
  if (!scene) return null;
  // Try the documented main viewport keys across 0.25.x, then any scrolled one.
  const keys = ["viewMain", "VIEW_MAIN", "sheetViewMain"];
  for (const k of keys) {
    try {
      const vp = scene.getViewport && scene.getViewport(k);
      if (vp) return vp;
    } catch (_e) { /* try next */ }
  }
  try {
    const all = scene.getViewports && scene.getViewports();
    if (Array.isArray(all)) {
      return all.find((v: any) => v && (v.viewportScrollX || v.viewportScrollY)) || all[0];
    }
  } catch (_e) { /* noop */ }
  return null;
}

// ---------------------------------------------------------------------------
// Dev probe: paste the body of this into devtools once in the BUILT app to
// confirm projectCell against a known cell (e.g. B2). It logs the skeleton rect
// and the on-screen canvas rect so you can eyeball the transform.
// ---------------------------------------------------------------------------

export function probeProjection(univer: any, unitId: string, row = 1, col = 1): void {
  try {
    const inj = univer.__getInjector();
    const g: any = (globalThis as any).UniverCore || (globalThis as any).Univer || globalThis;
    const rms = inj.get(g.IRenderManagerService);
    const render = rms.getRenderById(unitId);
    const skm = (render.with && g.SheetSkeletonManagerService)
      ? render.with(g.SheetSkeletonManagerService)
      : inj.get(g.SheetSkeletonManagerService);
    const sk = skm.getCurrentSkeleton();
    const cell = sk.getCellWithCoordByIndex(row, col);
    const scene = render.scene;
    const vp = pickMainViewport(scene);
    const m = scene.transform.getMatrix();
    const scroll = (vp && typeof vp.getViewportScrollByScrollXY === "function")
      ? vp.getViewportScrollByScrollXY()
      : { x: (vp && vp.viewportScrollX) || 0, y: (vp && vp.viewportScrollY) || 0 };
    // eslint-disable-next-line no-console
    console.log("[probe] cell(scene)", cell,
      "matrix", m, "scroll", scroll,
      "viewport", vp && { left: vp.left, top: vp.top, width: vp.width, height: vp.height },
      "-> projected", projectCell(cell, m, scroll.x, scroll.y));
  } catch (e) {
    // eslint-disable-next-line no-console
    console.error("[probe] failed", e);
  }
}
