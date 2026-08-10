# Vendored ECharts

`echarts.min.js` — Apache ECharts **5.6.0**, the official self-contained UMD
build (`dist/echarts.min.js` from the npm package). Assigns `window.echarts`.

Vendored rather than built from source because the offline package set does not
include ECharts' runtime dependencies (`zrender`, `tslib`), so a tree-shaken
Vite library build cannot be produced here. This mirrors how the rest of the
frontend is shipped: a prebuilt bundle served from the Flask static tree, no CDN,
no network at runtime.

Size is ~1.0 MB, which is ~9% of the Univer bundle already served by the editor
page — and the dashboard is the only page that loads it.

## How it is used

The page loads two files, in order:

```html
<script src="/static/vendor/charts/echarts.min.js"></script>
<script src="/static/js/lanmatrix/charts_theme.js"></script>
```

`charts_theme.js` derives the chart theme from the app's own CSS custom
properties and exposes `window.LMCharts`. See the comments in that file for why
the theme is computed from tokens instead of hard-coded.

**Optional and non-breaking:** if `echarts.min.js` is missing, `LMCharts.ready()`
returns `false` and the dashboard falls back to numeric summary cards instead of
rendering an empty page.

## Upgrading

Replace `echarts.min.js` with a newer `dist/echarts.min.js`, update the version
above, and reload the dashboard. `charts_theme.js` uses only the stable
`echarts.init` / `registerTheme` / `setOption` API and needs no change.

`LICENSE` — Apache License 2.0, as distributed with the package.
`echarts.package.json` — the upstream manifest, kept for provenance.
