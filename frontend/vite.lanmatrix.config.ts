import { build, defineConfig, Plugin } from "vite";
import { resolve } from "path";

const outDir = resolve(__dirname, "../app/static/js/lanmatrix");

function buildSecondaryLanmatrixEntries(): Plugin {
  let builtSecondary = false;
  return {
    name: "build-secondary-lanmatrix-entries",
    async closeBundle() {
      if (builtSecondary) return;
      builtSecondary = true;
      await build({
        configFile: false,
        esbuild: {
          legalComments: "none",
          drop: ["debugger"],
        },
        build: {
          outDir,
          emptyOutDir: false,
          cssCodeSplit: false,
          target: "es2020",
          minify: false,
          lib: {
            entry: resolve(__dirname, "src/lanmatrix/editor_qualification.ts"),
            name: "LMEditorQualificationBundle",
            formats: ["iife"],
            fileName: () => "editor_qualification.js",
          },
          rollupOptions: {
            output: {
              inlineDynamicImports: true,
            },
          },
        },
      });
    },
  };
}

export default defineConfig({
  plugins: [buildSecondaryLanmatrixEntries()],
  esbuild: {
    legalComments: "none",
    drop: ["debugger"],
  },
  build: {
    outDir,
    emptyOutDir: false,
    cssCodeSplit: false,
    target: "es2020",
    minify: false,
    lib: {
      entry: resolve(__dirname, "src/lanmatrix/ut_tasks.ts"),
      name: "LMUtTasksBundle",
      formats: ["iife"],
      fileName: () => "ut_tasks.js",
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
