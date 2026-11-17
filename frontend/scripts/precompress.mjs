import { existsSync, readFileSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { constants as zlibConstants, gzipSync } from "node:zlib";

const __dirname = dirname(fileURLToPath(import.meta.url));
const vendorRoot = resolve(__dirname, "../../app/static/vendor");

const targets = [
  "univer/univer.full.umd.js",
  "univer/univer.full.umd.css",
  "collab/collab.umd.js",
  "sbs/sbs-editor.umd.js",
  "sbs/onig.wasm",
  "sbs/sbsV202403.tmLanguage.json",
  "charts/echarts.min.js",
];

for (const rel of targets) {
  const src = resolve(vendorRoot, rel);
  if (!existsSync(src)) continue;
  const raw = readFileSync(src);
  if (raw.byteLength < 2048) continue;
  const gz = gzipSync(raw, { level: zlibConstants.Z_BEST_COMPRESSION });
  const dst = `${src}.gz`;
  writeFileSync(dst, gz);
  const st = statSync(src);
  const mtimeSec = st.mtimeMs / 1000 + 0.001;
  utimesSync(dst, mtimeSec, mtimeSec);
  const ratio = ((1 - gz.byteLength / raw.byteLength) * 100).toFixed(1);
  console.log(
    `[precompress] ${rel}: ${(raw.byteLength / 1024).toFixed(1)} KiB -> ${(
      gz.byteLength / 1024
    ).toFixed(1)} KiB (-${ratio}%)`
  );
}
