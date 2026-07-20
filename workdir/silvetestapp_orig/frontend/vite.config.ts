import { defineConfig } from "vite";
import { resolve } from "path";

/**
 * Library build: bundle Univer Sheets + the LAN Test Matrix adapter into a
 * single self-executing (IIFE) script plus one CSS file, emitted straight into
 * the Flask static tree so it can be served offline with no CDN dependency.
 *
 *   app/static/vendor/univer/univer.full.umd.js
 *   app/static/vendor/univer/univer.full.umd.css
 *
 * The bundle assigns `window.LMUniver = { mount }`, which `grid.js`
 * auto-detects. If the bundle is absent the editor silently uses its built-in
 * grid, so shipping this is always optional and non-breaking.
 */
export default defineConfig({
  // Univer and some of its deps read `process.env.NODE_ENV` (and, rarely, other
  // `process.*` / `global`) at runtime. In a browser those globals don't exist,
  // which throws "process is not defined" and aborts Univer's bootstrap — so the
  // grid silently falls back. Replace them at build time here…
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
    global: "globalThis",
  },
  build: {
    outDir: resolve(__dirname, "../app/static/vendor/univer"),
    emptyOutDir: false, // keep README.md and any hand-vendored assets
    cssCodeSplit: false,
    target: "es2018",
    minify: true,
    lib: {
      entry: resolve(__dirname, "src/main.ts"),
      name: "LMUniverBundle",
      formats: ["iife"],
      fileName: () => "univer.full.umd.js",
    },
    rollupOptions: {
      output: {
        // …and inject a tiny runtime shim ahead of the IIFE as a safety net for
        // any bare `process` / `global` reference the define step can't rewrite
        // statically (e.g. computed access). Runs before any bundled code.
        banner:
          "window.global=window.global||window;" +
          "window.process=window.process||{env:{NODE_ENV:\"production\"}};",
        inlineDynamicImports: true,
        assetFileNames: (info) => {
          const name = (info as any).name || "";
          if (name.endsWith(".css")) return "univer.full.umd.css";
          return "assets/[name][extname]";
        },
      },
    },
  },
});
