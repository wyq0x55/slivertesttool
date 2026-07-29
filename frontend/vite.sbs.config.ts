import { defineConfig } from "vite";
import { resolve } from "path";

/**
 * Library build: bundle CodeMirror 6 + vscode-textmate + vscode-oniguruma into
 * a single self-executing (IIFE) script emitted straight into the Flask static
 * tree so the in-app SBS editor works fully offline with no CDN dependency.
 *
 *   app/static/vendor/sbs/sbs-editor.umd.js
 *
 * The bundle assigns `window.LMSbsEditor = { mount }`, which
 * app/static/js/lanmatrix/sbs_editor.js auto-detects. If the bundle is absent
 * the controller falls back to a plain <textarea>, so shipping this is always
 * optional and non-breaking.
 *
 * The TextMate grammar (sbsV202403.tmLanguage.json) and the Oniguruma WASM are
 * NOT bundled -- they are fetched at runtime from app/static/vendor/sbs/ (URLs
 * passed by the controller), so this build is decoupled from the serving path.
 *
 * This is a SEPARATE Vite config from vite.config.ts (Univer) and
 * vite.collab.config.ts because a Vite library build emits a single entry per
 * config. Build all three with `npm run build`, or just this one with
 * `npm run build:sbs`.
 */
export default defineConfig({
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
    global: "globalThis",
  },
  build: {
    outDir: resolve(__dirname, "../app/static/vendor/sbs"),
    emptyOutDir: false, // keep the vendored grammar + language-configuration
    cssCodeSplit: false,
    target: "es2018",
    minify: true,
    lib: {
      entry: resolve(__dirname, "src/sbs_editor.ts"),
      name: "LMSbsEditorBundle",
      formats: ["iife"],
      fileName: () => "sbs-editor.umd.js",
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
