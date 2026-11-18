import {
  existsSync,
  readdirSync,
  readFileSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { dirname, relative, resolve } from "node:path";
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

function compressAsset(src, label, minBytes = 2048) {
  if (!existsSync(src)) return;
  const raw = readFileSync(src);
  if (raw.byteLength < minBytes) return;
  const gz = gzipSync(raw, { level: zlibConstants.Z_BEST_COMPRESSION });
  const dst = `${src}.gz`;
  writeFileSync(dst, gz);
  const st = statSync(src);
  const mtimeSec = st.mtimeMs / 1000 + 0.01;
  utimesSync(dst, mtimeSec, mtimeSec);
  const ratio = ((1 - gz.byteLength / raw.byteLength) * 100).toFixed(1);
  console.log(
    `[precompress] ${label}: ${(raw.byteLength / 1024).toFixed(1)} KiB -> ${(
      gz.byteLength / 1024
    ).toFixed(1)} KiB (-${ratio}%)`
  );
}

function collectEsmAssets(dir, acc = []) {
  if (!existsSync(dir)) return acc;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = resolve(dir, entry.name);
    if (entry.isDirectory()) {
      collectEsmAssets(full, acc);
    } else if (entry.isFile() && (entry.name.endsWith(".js") || entry.name.endsWith(".css"))) {
      acc.push(full);
    }
  }
  return acc;
}

for (const rel of targets) {
  compressAsset(resolve(vendorRoot, rel), rel, 2048);
}

const esmRoot = resolve(vendorRoot, "univer/esm");
for (const esmFile of collectEsmAssets(esmRoot)) {
  const relLabel = relative(vendorRoot, esmFile).replace(/\\/g, "/");
  compressAsset(esmFile, relLabel, 1);
}
