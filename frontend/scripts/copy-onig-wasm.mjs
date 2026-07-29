// Copy the Oniguruma WASM regex engine next to the SBS grammar so the
// browser can fetch it at runtime (app/static/vendor/sbs/onig.wasm). The SBS
// editor bundle intentionally does NOT inline the wasm; it fetches this file.
import { createRequire } from "module";
import { copyFileSync, mkdirSync } from "fs";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));

const src = require.resolve("vscode-oniguruma/release/onig.wasm");
const dest = resolve(here, "../../app/static/vendor/sbs/onig.wasm");

mkdirSync(dirname(dest), { recursive: true });
copyFileSync(src, dest);
console.log("copied onig.wasm ->", dest);
