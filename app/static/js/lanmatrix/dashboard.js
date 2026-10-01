/* Project dashboard.
 *
 * Renders four figures from one bundled snapshot:
 *   - 实施进度   coverage of the in-scope matrix
 *   - 审核漏斗   how much of "done" is actually signed off
 *   - 累计实施趋势 whether progress is still moving
 *   - 按版本对比 how each model version performed
 *
 * Two rules shape the code:
 *
 * 1. **Numbers before pictures.** The KPI counters render from the same
 *    snapshot and never depend on the chart bundle. If ECharts is missing the
 *    page still answers the question, just without the visualisation. A
 *    dashboard whose failure mode is a blank screen is worse than one that
 *    degrades to a table.
 *
 * 2. **No colour literals.** Every series colour comes from LMCharts.palette(),
 *    which reads the design-system tokens. That is also why charts are rebuilt
 *    on the `lm-chart-rebuilt` event: the theme bridge disposes and recreates
 *    instances on theme switch, so the handles held here must be refreshed.
 */
(function (global) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const root = document.querySelector(".lm-dash");
  if (!root) return;

  const projectId = root.dataset.project;
  const charts = {};
  let snapshot = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function chartsReady() {
    return !!(global.LMCharts && LMCharts.ready());
  }

  function setKpi(key, value) {
    const el = document.querySelector(`#lm-dash-kpi [data-v="${key}"]`);
    if (el) el.textContent = value;
  }

  function renderKpis(s, review) {
    setKpi("total", s.total);
    setKpi("planned", s.planned);
    setKpi("executed", s.executed);
    setKpi("passed", s.passed);
    setKpi("failed_errored", s.failed + s.errored);
    setKpi("review_pending", review.pending);
    // Approved 不要 cases have left the denominator; pending ones have not, and
    // saying so is the point of the tile. Silence about a pending claim is how
    // a case ends up excluded without anyone having agreed to it.
    setKpi("exempt_approved", s.exempt_approved || 0);
    setKpi("exempt_pending",
      s.exempt_pending ? `${s.exempt_pending} 待审批（仍计入要实施）` : "");
    // Percentages are shown against the in-scope total, so state the
    // denominator rather than leaving a bare "72%".
    setKpi("executed_pct", s.planned ? `${s.executed_pct}% / 要实施` : "");
    setKpi("passed_pct", s.planned ? `${s.passed_pct}% / 要实施` : "");
  }

  /* Build a chart in `elId`, tracking the handle and re-binding it when the
   * theme bridge rebuilds the instance. */
  function build(elId, optionFn) {
    const el = $(elId);
    if (!el || !chartsReady()) return;
    const chart = LMCharts.init(el);
    if (!chart) return;
    charts[elId] = chart;
    chart.setOption(optionFn(LMCharts.palette()), true);
    if (!el.dataset.rebindBound) {
      el.dataset.rebindBound = "1";
      el.addEventListener("lm-chart-rebuilt", (e) => {
        charts[elId] = e.detail.chart;
      });
    }
  }

  function progressOption(p) {
    const s = snapshot.summary;
    // Ordered worst-to-best around the ring so the eye lands on the gap first.
    const data = [
      { name: "通过", value: s.passed, itemStyle: { color: p.ok } },
      { name: "失败", value: s.failed, itemStyle: { color: p.danger } },
      { name: "错误", value: s.errored, itemStyle: { color: p.warn } },
      { name: "无法测试", value: s.untestable, itemStyle: { color: p.info } },
      // Split out of 未实施 rather than added to it: these cases are proposed
      // for removal from the plan and are still counted in the denominator, so
      // burying them in the grey block hides both the backlog and the pending
      // decision that would shrink it.
      {
        name: "不要待审批",
        value: s.exempt_pending_not_run || 0,
        itemStyle: { color: p.warn },
      },
      {
        name: "未实施",
        value: Math.max(0, s.not_run - (s.exempt_pending_not_run || 0)),
        itemStyle: { color: p.muted },
      },
    ].filter((d) => d.value > 0);

    return {
      tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
      legend: { bottom: 0, icon: "circle" },
      series: [{
        type: "pie",
        radius: ["52%", "74%"],
        center: ["50%", "44%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: p.surface, borderWidth: 2 },
        label: {
          show: true,
          position: "center",
          // The ring's centre carries the headline the page exists to deliver.
          formatter: () => `{a|${s.executed_pct}%}\n{b|已实施}`,
          rich: {
            a: { fontSize: 26, fontWeight: 600, color: p.text },
            b: { fontSize: 12, color: p.muted, padding: [4, 0, 0, 0] },
          },
        },
        emphasis: { label: { show: true } },
        labelLine: { show: false },
        data,
      }],
    };
  }

  function reviewOption(p) {
    const r = snapshot.review;
    return {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      grid: { left: 70, right: 24, top: 20, bottom: 30 },
      xAxis: { type: "value", minInterval: 1 },
      yAxis: {
        type: "category",
        // Reversed so the funnel reads top-down: needs review -> decided.
        data: ["已驳回", "已通过", "待审核"],
      },
      series: [{
        type: "bar",
        barWidth: "52%",
        itemStyle: { borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: "right", color: p.text },
        data: [
          { value: r.rejected, itemStyle: { color: p.danger } },
          { value: r.approved, itemStyle: { color: p.ok } },
          { value: r.pending, itemStyle: { color: p.warn } },
        ],
      }],
    };
  }

  function trendOption(p) {
    const t = snapshot.trend;
    return {
      tooltip: { trigger: "axis" },
      legend: { bottom: 0, data: ["累计已实施", "当日实施"] },
      grid: { left: 56, right: 24, top: 20, bottom: 52 },
      xAxis: { type: "category", boundaryGap: false, data: t.dates },
      yAxis: [
        { type: "value", minInterval: 1 },
        { type: "value", minInterval: 1, splitLine: { show: false } },
      ],
      series: [
        {
          name: "累计已实施",
          type: "line",
          smooth: true,
          showSymbol: t.dates.length <= 40,
          lineStyle: { width: 2, color: p.brand },
          itemStyle: { color: p.brand },
          areaStyle: { color: p.brand, opacity: 0.12 },
          data: t.cumulative,
        },
        {
          // Daily volume is the momentum signal: a flat cumulative line and an
          // empty bar row mean different things to whoever has to act.
          name: "当日实施",
          type: "bar",
          yAxisIndex: 1,
          barMaxWidth: 18,
          itemStyle: { color: p.muted, opacity: 0.5, borderRadius: [3, 3, 0, 0] },
          data: t.daily,
        },
      ],
    };
  }

  function versionOption(p) {
    const v = snapshot.by_version;
    const defs = [
      ["pass", "通过", p.ok],
      ["fail", "失败", p.danger],
      ["error", "错误", p.warn],
      ["untestable", "无法测试", p.info],
      ["cancelled", "已取消", p.muted],
    ];
    return {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      legend: { bottom: 0 },
      grid: { left: 56, right: 24, top: 20, bottom: 52 },
      xAxis: { type: "category", data: v.versions },
      yAxis: { type: "value", minInterval: 1 },
      series: defs.map(([key, label, color]) => ({
        name: label,
        type: "bar",
        stack: "v",
        barMaxWidth: 48,
        itemStyle: { color },
        emphasis: { focus: "series" },
        data: (v.series && v.series[key]) || [],
      })),
    };
  }

  function renderCharts() {
    if (!chartsReady()) return;
    build("lm-chart-progress", progressOption);
    build("lm-chart-review", reviewOption);
    build("lm-chart-trend", trendOption);
    build("lm-chart-version", versionOption);
  }

  function showError(msg) {
    const box = $("lm-dash-err");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = esc(msg);
  }

  async function load() {
    const btn = $("lm-dash-refresh");
    if (btn) btn.disabled = true;
    try {
      snapshot = await LMApi.projectDashboard(projectId);
      const err = $("lm-dash-err");
      if (err) err.hidden = true;

      renderKpis(snapshot.summary, snapshot.review);

      // "No data" is about the matrix being empty, not about charts failing.
      const empty = snapshot.summary.total === 0;
      const emptyBox = $("lm-dash-empty");
      const grid = $("lm-dash-charts");
      if (emptyBox) emptyBox.hidden = !empty;
      if (grid && chartsReady()) grid.hidden = empty;

      if (!empty) renderCharts();
    } catch (ex) {
      if (ex.status === 401) return; // the API layer handles the redirect
      showError(`加载失败：${ex.message || "未知错误"}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

    const COMPARE_LABELS = {
    only_left: "仅左侧",
    only_right: "仅右侧",
    unchanged: "无变化",
    pass_to_fail: "通过转失败",
    fail_to_pass: "失败转通过",
    other: "其他",
  };
  const OUTCOME_LABELS = {
    pass: "通过",
    fail: "失败",
    error: "错误",
    untestable: "无法测试",
  };
  let comparePage = 1;
  let compareRequestId = 0;
  let historyPage = 1;
  let historyTestId = "";
  let historyRequestId = 0;

  function outcomeText(value) {
    if (!value) return "\u2014";
    return OUTCOME_LABELS[value] || value;
  }

  function matrixRowHref(rowUuid) {
    if (!rowUuid) return "";
    return "/lanmatrix/projects/" + encodeURIComponent(projectId)
      + "?row=" + encodeURIComponent(rowUuid);
  }

  function renderCompareRow(item) {
    const href = matrixRowHref(item.row_uuid);
    const label = esc(item.test_id);
    const testCell = href
      ? '<a href="' + esc(href) + '">' + label + "</a>"
      : label;
    const historyButton = '<button type="button" class="btn" data-history-test-id="'
      + esc(item.test_id) + '" aria-controls="lm-vc-history">历史</button>';
    return "<tr><td>" + testCell + " " + historyButton + "</td><td>"
      + esc(outcomeText(item.left_outcome)) + "</td><td>"
      + esc(outcomeText(item.right_outcome)) + "</td><td>"
      + esc(COMPARE_LABELS[item.change] || item.change || "")
      + "</td></tr>";
  }

  function renderCompare(data) {
    const summary = data.summary || {};
    const summaryEl = $("lm-vc-summary");
    if (summaryEl) {
      summaryEl.textContent = [
        "仅左侧 " + (summary.only_left || 0),
        "仅右侧 " + (summary.only_right || 0),
        "无变化 " + (summary.unchanged || 0),
        "通过转失败 " + (summary.pass_to_fail || 0),
        "失败转通过 " + (summary.fail_to_pass || 0),
        "其他 " + (summary.other || 0),
      ].join("，");
    }
    const body = $("lm-vc-rows");
    const items = data.items || [];
    if (body) {
      body.innerHTML = items.length
        ? items.map(renderCompareRow).join("")
        : '<tr><td colspan="4" class="lm-muted">没有可对比的用例。</td></tr>';
    }
    const size = data.page_size || 50;
    const page = data.page || comparePage;
    const total = data.total || 0;
    const prev = $("lm-vc-prev");
    const next = $("lm-vc-next");
    if (prev) prev.hidden = page <= 1;
    if (next) next.hidden = page * size >= total;
  }

  function showCompareError(message) {
    const box = $("lm-vc-err");
    if (!box) return;
    box.hidden = false;
    box.textContent = message;
  }

  function renderRunHistoryRow(item) {
    const rowHref = matrixRowHref(item.row_uuid);
    const rowLink = rowHref
      ? '<a href="' + esc(rowHref) + '">打开矩阵</a>'
      : "—";
    const model = item.model_version
      ? String(item.model_name || "") + "@" + String(item.model_version)
      : String(item.model_name || "—");
    return "<tr><td>" + esc(item.executed_at || item.executed_on || "—")
      + "</td><td>" + esc(outcomeText(item.outcome || item.verdict))
      + "</td><td>" + esc(model)
      + "</td><td>" + esc(item.executor_name || "—")
      + "</td><td>" + rowLink + "</td></tr>";
  }

  function renderRunHistory(data) {
    const title = $("lm-vc-history-title");
    const status = $("lm-vc-history-status");
    const body = $("lm-vc-history-rows");
    const items = data.items || [];
    if (title) title.textContent = "用例运行历史 · " + (data.test_id || historyTestId);
    if (body) {
      body.innerHTML = items.length
        ? items.map(renderRunHistoryRow).join("")
        : '<tr><td colspan="5" class="lm-muted">此用例暂无矩阵运行记录。</td></tr>';
    }
    const page = data.page || historyPage;
    const size = data.page_size || 50;
    const total = data.total || 0;
    if (status) status.textContent = "共 " + total + " 条记录，第 " + page + " 页";
    const previous = $("lm-vc-history-prev");
    const next = $("lm-vc-history-next");
    if (previous) previous.hidden = page <= 1;
    if (next) next.hidden = page * size >= total;
  }

  function showRunHistoryError(message) {
    const status = $("lm-vc-history-status");
    const box = $("lm-vc-history-error");
    if (status) status.textContent = "";
    if (box) {
      box.hidden = false;
      box.textContent = message;
    }
  }

  async function loadRunHistory(page) {
    const panel = $("lm-vc-history");
    if (!panel || !historyTestId) return;
    const requestId = ++historyRequestId;
    const requestedTestId = historyTestId;
    panel.hidden = false;
    historyPage = page || 1;
    const error = $("lm-vc-history-error");
    const status = $("lm-vc-history-status");
    const body = $("lm-vc-history-rows");
    if (error) error.hidden = true;
    if (status) status.textContent = "正在加载运行历史…";
    if (body) body.innerHTML = "";
    try {
      const query = new URLSearchParams({
        test_id: historyTestId,
        page: String(historyPage),
        page_size: "50",
      });
      const resp = await fetch(
        "/api/v1/projects/" + encodeURIComponent(projectId)
          + "/test-run-history?" + query.toString(),
        { credentials: "same-origin" },
      );
      let payload = null;
      try { payload = await resp.json(); } catch (ignore) { payload = null; }
      if (requestId !== historyRequestId || requestedTestId !== historyTestId) return;
      if (resp.status === 401) {
        showRunHistoryError("登录已过期，请刷新页面后重新登录。");
        return;
      }
      if (!payload || !payload.success) {
        const failure = (payload && payload.error) || {};
        showRunHistoryError(failure.message || "读取运行历史失败");
        return;
      }
      renderRunHistory(payload.data || {});
    } catch (ignore) {
      if (requestId === historyRequestId && requestedTestId === historyTestId) {
        showRunHistoryError("读取运行历史失败");
      }
    }
  }

  function bindRunHistory() {
    const rows = $("lm-vc-rows");
    if (rows) {
      rows.addEventListener("click", (event) => {
        const target = event.target;
        if (!target || typeof target.closest !== "function") return;
        const button = target.closest("[data-history-test-id]");
        if (!button) return;
        historyTestId = button.dataset.historyTestId || "";
        historyPage = 1;
        loadRunHistory(1);
      });
    }
    const previous = $("lm-vc-history-prev");
    const next = $("lm-vc-history-next");
    if (previous) previous.addEventListener("click", () => {
      loadRunHistory(Math.max(1, historyPage - 1));
    });
    if (next) next.addEventListener("click", () => {
      loadRunHistory(historyPage + 1);
    });
  }

  async function loadVersionCompare(page) {
    const requestId = ++compareRequestId;
    clearCompareResults();
    const left = ($("lm-vc-left") && $("lm-vc-left").value.trim()) || "";
    const right = ($("lm-vc-right") && $("lm-vc-right").value.trim()) || "";
    const button = $("lm-vc-run");
    const isCurrent = () => requestId === compareRequestId &&
      left === ($("lm-vc-left")?.value.trim() || "") &&
      right === ($("lm-vc-right")?.value.trim() || "");
    if (!left || !right) {
      if (button) button.disabled = false;
      showCompareError("请填写左侧和右侧模型，格式为 name@version，没有版本时只填名称。");
      return;
    }
    comparePage = page || 1;
    if (button) button.disabled = true;
    try {
      const query = new URLSearchParams({
        left: left,
        right: right,
        page: String(comparePage),
        page_size: "50",
      });
      const resp = await fetch(
        "/api/v1/projects/" + encodeURIComponent(projectId) + "/version-compare?" + query.toString(),
        { credentials: "same-origin" },
      );
      let payload = null;
      try { payload = await resp.json(); } catch (ignore) { payload = null; }
      if (!isCurrent()) return;
      if (resp.status === 401) {
        showCompareError("登录已过期，请刷新页面后重新登录。");
        return;
      }
      if (!resp.ok || !payload || !payload.success) {
        const failure = (payload && payload.error) || {};
        showCompareError(failure.message || "对比失败");
        return;
      }
      const errBox = $("lm-vc-err");
      if (errBox) errBox.hidden = true;
      renderCompare(payload.data || {});
    } catch (ignore) {
      if (isCurrent()) showCompareError("对比失败");
    } finally {
      if (requestId === compareRequestId && button) button.disabled = false;
    }
  }

  function clearCompareResults() {
    const summary = $("lm-vc-summary");
    const rows = $("lm-vc-rows");
    const error = $("lm-vc-err");
    if (summary) summary.textContent = "";
    if (rows) rows.innerHTML = "";
    if (error) error.hidden = true;
    ["lm-vc-prev", "lm-vc-next", "lm-vc-history"].forEach((id) => {
      const element = $(id);
      if (element) element.hidden = true;
    });
    historyRequestId += 1;
    historyTestId = "";
  }

  function bindVersionCompare() {
    ["lm-vc-left", "lm-vc-right"].forEach((id) => {
      const input = $(id);
      if (input) input.addEventListener("input", () => {
        compareRequestId += 1;
        comparePage = 1;
        clearCompareResults();
        const button = $("lm-vc-run");
        if (button) button.disabled = false;
      });
    });
    const form = $("lm-vc-form");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        loadVersionCompare(1);
      });
    }
    const prev = $("lm-vc-prev");
    const next = $("lm-vc-next");
    if (prev) prev.addEventListener("click", () => loadVersionCompare(Math.max(1, comparePage - 1)));
    if (next) next.addEventListener("click", () => loadVersionCompare(comparePage + 1));
  }

  document.addEventListener("DOMContentLoaded", () => {
    const btn = $("lm-dash-refresh");
    if (btn) btn.addEventListener("click", load);
    load();
    bindVersionCompare();
    bindRunHistory();
  });

  global.LMDashboard = { reload: load };
})(window);
