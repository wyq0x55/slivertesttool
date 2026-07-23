#!/usr/bin/env bash
# Build the Univer Sheets bundle into app/static/vendor/univer/.
# Run on a machine that has Node.js >= 18 and the npm deps available
# (either online, a local npm registry, or a vendored node_modules/).
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm not found. Install Node.js >= 18 (or vendor node_modules/)." >&2
  exit 1
fi

if [ ! -d node_modules ]; then
  echo ">> installing dependencies (npm install)"
  npm install
fi

echo ">> building Univer bundle (vite build)"
npm run build

OUT="../app/static/vendor/univer"
echo ">> output:"
ls -l "$OUT/univer.full.umd.js" "$OUT/univer.full.umd.css" 2>/dev/null || {
  echo "ERROR: expected bundle files were not produced." >&2
  exit 2
}
echo ">> done. Restart the Flask app; the editor status bar should read '· Univer 引擎'."
