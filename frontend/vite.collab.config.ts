import { defineConfig } from "vite";
import { resolve } from "path";

/**
 * Library build: bundle the Yjs CRDT runtime + y-websocket provider into a
 * single self-executing (IIFE) script emitted straight into the Flask static
 * tree so real-time collaboration works fully offline with no CDN dependency.
 *
 *   app/static/vendor/collab/collab.umd.js
 *
 * The bundle assigns `window.LMCollab = { Y, WebsocketProvider, Awareness }`,
 * which app/static/js/lanmatrix/collab.js auto-detects. If the bundle is absent
 * the editor silently stays in classic REST + polling mode, so shipping this is
 * always optional and non-breaking.
 *
 * This is a SEPARATE Vite config from vite.config.ts (Univer) because a Vite
 * library build emits a single entry per config. Build both with `npm run build`
 * (see package.json), or just this one with `npm run build:collab`.
 */
export default defineConfig({
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
    global: "globalThis",
  },
  build: {
    outDir: resolve(__dirname, "../app/static/vendor/collab"),
    emptyOutDir: false, // keep README.md and any hand-vendored assets
    cssCodeSplit: false,
    target: "es2018",
    minify: true,
    lib: {
      entry: resolve(__dirname, "src/collab.ts"),
      name: "LMCollabBundle",
      formats: ["iife"],
      fileName: () => "collab.umd.js",
    },
    rollupOptions: {
      output: {
        banner:
          "window.global=window.global||window;" +
          "window.process=window.process||{env:{NODE_ENV:\"production\"}};",
        inlineDynamicImports: true,
      },
    },
  },
});
