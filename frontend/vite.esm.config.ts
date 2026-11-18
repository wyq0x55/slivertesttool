import { defineConfig, Plugin } from "vite";
import { resolve } from "path";

/**
 * Strip CSS imports inside the ESM code-split build because `editor.html`
 * already links `univer.full.umd.css` up front (zero FOUC, no runtime CSS
 * preload helper needed for dynamic JS chunks).
 */
function ignoreCssInEsm(): Plugin {
  return {
    name: "ignore-css-in-esm",
    enforce: "pre",
    load(id) {
      const cleanId = id.split("?", 1)[0];
      if (cleanId.endsWith(".css")) {
        return "";
      }
      return null;
    },
  };
}

export default defineConfig({
  plugins: [ignoreCssInEsm()],
  resolve: {
    dedupe: ["react", "react-dom"],
  },
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
    global: "globalThis",
  },
  esbuild: {
    legalComments: "none",
    drop: ["debugger"],
  },
  build: {
    outDir: resolve(__dirname, "../app/static/vendor/univer/esm"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 8000,
    cssCodeSplit: false,
    modulePreload: false,
    target: "es2020",
    minify: true,
    rollupOptions: {
      input: resolve(__dirname, "src/univer-esm-entry.ts"),
      preserveEntrySignatures: false,
      output: {
        format: "es",
        banner:
          "if(typeof window!==\"undefined\"){" +
          "window.global=window.global||window;" +
          "window.process=window.process||{env:{NODE_ENV:\"production\"}};" +
          "}",
        entryFileNames: "univer-core.js",
        chunkFileNames: "chunks/[name]-[hash].js",
      },
    },
  },
});
