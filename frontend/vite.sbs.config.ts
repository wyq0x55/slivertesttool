import { defineConfig } from "vite";
import { createRequire } from "module";
import { copyFileSync, mkdirSync } from "fs";
import { dirname, resolve } from "path";

const require = createRequire(import.meta.url);

/**
 * Vite plugin: after the bundle is written, copy the Oniguruma WASM regex
 * engine next to the vendored SBS grammar so the browser can fetch it at
 * runtime (app/static/vendor/sbs/onig.wasm). The editor bundle intentionally
 * does NOT inline the wasm; it fetches this file. Doing the copy here (instead
 * of a separate npm script) keeps the build self-contained -- no external
 * script file to go missing.
 */
function copyOnigWasm() {
  return {
    name: "copy-onig-wasm",
    closeBundle() {
      const src = require.resolve("vscode-oniguruma/release/onig.wasm");
      const dest = resolve(__dirname, "../app/static/vendor/sbs/onig.wasm");
      mkdirSync(dirname(dest), { recursive: true });
      copyFileSync(src, dest);
      // eslint-disable-next-line no-console
      console.log("copied onig.wasm ->", dest);
    },
  };
}

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
  plugins: [copyOnigWasm()],
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
