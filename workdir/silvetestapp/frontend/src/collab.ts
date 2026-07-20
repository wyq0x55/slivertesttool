/*
 * Entry point for the real-time collaboration (Yjs) bundle.
 *
 * Vite compiles this (with Yjs + y-websocket inlined) into
 *   app/static/vendor/collab/collab.umd.js
 * and it assigns `window.LMCollab = { Y, WebsocketProvider, Awareness }`.
 *
 * All session / binding logic lives in the hand-written, build-free
 * app/static/js/lanmatrix/collab.js controller; this bundle only ships the
 * third-party CRDT runtime so the Flask app can serve it fully offline (no
 * CDN). If the bundle is absent, collab.js detects `window.LMCollab` is
 * missing and the editor stays in its classic REST + polling mode — shipping
 * this is always optional and non-breaking.
 */
import * as Y from "yjs";
import { WebsocketProvider } from "y-websocket";
import { Awareness } from "y-protocols/awareness";

declare global {
  interface Window {
    LMCollab?: {
      Y: typeof Y;
      WebsocketProvider: typeof WebsocketProvider;
      Awareness: typeof Awareness;
    };
  }
}

window.LMCollab = { Y, WebsocketProvider, Awareness };
