/* Review policy editor (project settings -> 成员管理).
 *
 * Three things are decided here, and they belong together because each one is
 * useless without the others:
 *   1. which verdicts must be confirmed by a reviewer,
 *   2. who reviews each テスト区分 (ordered rules, first match wins),
 *   3. who reviews everything no rule claimed (the fallback reviewer).
 *
 * Per-区分 routing exists because a project-wide reviewer does not match how
 * the matrix is divided: each 区分 has its own feature owner, and routing
 * everything to one person makes them either a bottleneck or a rubber stamp.
 *
 * The panel stays hidden until we know the current user may change it: showing
 * disabled controls to a reader advertises a capability they do not have and
 * invites a support question.
 *
 * The policy is read from the dashboard snapshot endpoint rather than a
 * dedicated GET, because that payload already carries `review_policy` and a
 * second endpoint would be one more thing to keep in sync.
 */
(function (global) {
  "use strict";

  const panel = document.getElementById("lm-rv-policy");
  if (!panel) return;

  const root = document.querySelector(".lm-members");
  const projectId = root && root.getAttribute("data-project");
  if (!projectId) return;

  const passBox = document.getElementById("lm-rv-pol-pass");
  const untBox = document.getElementById("lm-rv-pol-untestable");
  const reviewerSel = document.getElementById("lm-rv-pol-reviewer");
  const saveBtn = document.getElementById("lm-rv-pol-save");
  const msg = document.getElementById("lm-rv-pol-msg");
  const routeHost = document.getElementById("lm-rv-route-rows");
  const routeAdd = document.getElementById("lm-rv-route-add");
  const routeHint = document.getElementById("lm-rv-route-hint");
  const catList = document.getElementById("lm-rv-cat-list");

  // Remembers what the server last confirmed, so a failed save can put the
  // controls back instead of leaving them showing a state that was rejected.
  let committed = {
    pass: false, untestable: true, default_reviewer_id: null, routes: [],
  };
  // Working copy of the routing rules; only `save` promotes it to `committed`.
  let routes = [];
  // { value, label } choices for the per-rule reviewer pickers, built from the members.
  let reviewerChoices = [{ value: "", label: "（选择审核人）" }];
  // Distinct テスト区分 that actually exist, for the picker and the coverage
  // hint. A rule typed against a 区分 that does not exist never fires, and
  // nothing else in the UI would ever say so.
  let categories = [];

  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null) continue;
        if (k === "className") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "list") node.setAttribute("list", v);
        else if (k === "dataset") {
          for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
        } else {
          node[k] = v;
        }
      }
    }
    if (children) node.append(...children);
    return node;
  }

  // Mirrors services/lanmatrix/review_routes.normalise_category: "01", "1.0"
  // and " 1 " are all 区分 1, so a rule typed as "1" must match them all.
  function normCat(raw) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) return "";
    const num = Number(text);
    if (!Number.isNaN(num) && Number.isInteger(num)) return String(num);
    return text;
  }

  function matches(pattern, category) {
    const p = String(pattern || "").trim();
    const c = normCat(category);
    if (!p || !c) return false;
    if (p === "*") return true;
    if (p.endsWith("*")) {
      return c.toLowerCase().startsWith(normCat(p.slice(0, -1)).toLowerCase());
    }
    return normCat(p).toLowerCase() === c.toLowerCase();
  }

  function paint(policy) {
    passBox.checked = !!policy.pass;
    untBox.checked = !!policy.untestable;
    if (reviewerSel) {
      reviewerSel.value = policy.default_reviewer_id
        ? String(policy.default_reviewer_id) : "";
    }
    routes = (policy.routes || []).map((r) => ({
      category: r.category,
      reviewer_id: r.reviewer_id,
      reviewer_name: r.reviewer_name || "",
    }));
    renderRoutes();
  }

  function renderRoutes() {
    if (!routeHost) return;
    if (!routes.length) {
      routeHost.replaceChildren(el("div", {
        className: "muted lm-rv-route-empty",
        text: "尚未设置区分规则，所有审核都会流向下方的默认审核人。",
      }));
    } else {
      const rows = routes.map((r, i) => {
        const want = r.reviewer_id ? String(r.reviewer_id) : "";
        const opts = reviewerChoices.map((c) =>
          el("option", { value: c.value, text: c.label }));
        // A reviewer removed from the project is no longer in the option list;
        // keep them selectable so the rule still renders as a name and can be
        // fixed, instead of silently reading as "unassigned".
        if (want && !reviewerChoices.some((c) => c.value === want)) {
          opts.push(el("option", {
            value: want,
            text: `${r.reviewer_name || ("#" + want)}（非成员）`,
          }));
        }
        const sel = el("select", { className: "input lm-rv-route-who" }, opts);
        sel.value = want;
        return el("div", { className: "lm-rv-route", dataset: { i: String(i) } }, [
          el("span", { className: "lm-rv-route-ord", text: String(i + 1) }),
          el("input", {
            className: "input lm-rv-route-cat",
            list: "lm-rv-cat-list",
            value: r.category == null ? "" : String(r.category),
            placeholder: "区分（如 5 或 1*）",
          }),
          sel,
          el("button", {
            type: "button",
            className: "btn btn-ghost lm-rv-route-up",
            title: "上移（提高优先级）",
            disabled: i === 0,
            text: "↑",
          }),
          el("button", {
            type: "button",
            className: "btn btn-ghost lm-rv-route-down",
            title: "下移",
            disabled: i === routes.length - 1,
            text: "↓",
          }),
          el("button", {
            type: "button",
            className: "btn btn-ghost lm-rv-route-del",
            title: "删除",
            text: "✕",
          }),
        ]);
      });
      routeHost.replaceChildren(...rows);
    }
    renderHint();
  }

  // Says which 区分 no rule covers. Coverage is the one property of this list a
  // user cannot check by reading it, and an uncovered 区分 silently falls
  // through to the default reviewer.
  function renderHint() {
    if (!routeHint) return;
    if (!categories.length) {
      routeHint.textContent = routes.length ? "未匹配的区分由默认审核人处理。" : "";
      return;
    }
    const uncovered = categories.filter(
      (c) => !routes.some((r) => matches(r.category, c.category)));
    if (!uncovered.length) {
      routeHint.textContent = `已覆盖全部 ${categories.length} 个区分。`;
      return;
    }
    const shown = uncovered.slice(0, 12).map(
      (c) => c.category + (c.category_name ? `·${c.category_name}` : ""));
    routeHint.textContent = `未覆盖区分：${shown.join("、")}`
      + (uncovered.length > shown.length ? ` 等 ${uncovered.length} 个` : "")
      + "，将由默认审核人处理。";
  }

  // The pickers are limited to project members (plus whoever is currently set),
  // matching the server-side check. Offering non-members would produce a 400 on
  // save and read as a bug rather than as the rule it is.
  async function loadReviewers(currentId, currentName) {
    let members = [];
    try {
      const data = await LMApi.listMembers(projectId);
      members = (data && data.members) || [];
    } catch (err) {
      // Not fatal: the panel is still usable for the checkboxes, and the
      // current reviewer is preserved as an option below.
    }
    const opts = [el("option", { value: "", text: "（未设置 · 回退项目负责人）" })];
    const routeOpts = [{ value: "", label: "（选择审核人）" }];
    const seen = new Set();
    members.forEach((m) => {
      if (!m.user_id || seen.has(m.user_id)) return;
      seen.add(m.user_id);
      const label = m.display_name || m.username || `#${m.user_id}`;
      const text = `${label}${m.role ? `（${m.role}）` : ""}`;
      const value = String(m.user_id);
      opts.push(el("option", { value: value, text: text }));
      routeOpts.push({ value: value, label: text });
    });
    // Keep the reviewer that is actually configured even if they are no longer
    // in the member list, so opening the panel cannot silently clear them.
    if (currentId && !seen.has(Number(currentId))) {
      opts.push(el("option", {
        value: String(currentId),
        text: `${currentName || `#${currentId}`}（非成员）`,
      }));
    }
    if (reviewerSel) reviewerSel.replaceChildren(...opts);
    reviewerChoices = routeOpts;
  }

  async function loadCategories() {
    try {
      const data = await LMApi.listProjectCategories(projectId);
      categories = (data && data.categories) || [];
    } catch (err) {
      categories = [];  // Picker degrades to free text; the rules still work.
    }
    if (catList) {
      const opts = categories.map((c) => {
        const label = c.category
          + (c.category_name ? ` · ${c.category_name}` : "")
          + `（${c.count} 条）`;
        return el("option", {
          value: c.category == null ? "" : String(c.category),
          text: label,
        });
      });
      catList.replaceChildren(...opts);
    }
  }

  function setMsg(text, kind) {
    msg.textContent = text || "";
    msg.className = kind === "error" ? "lm-rv-pol-err" : "muted";
  }

  // Reads the DOM back into `routes` before any structural change, so an edit
  // typed but not yet committed is not lost by pressing 上移 / 删除.
  function syncRoutesFromDom() {
    if (!routeHost) return;
    routeHost.querySelectorAll(".lm-rv-route").forEach((el) => {
      const i = Number(el.getAttribute("data-i"));
      if (!routes[i]) return;
      routes[i].category = el.querySelector(".lm-rv-route-cat").value.trim();
      const who = el.querySelector(".lm-rv-route-who").value;
      routes[i].reviewer_id = who ? Number(who) : null;
    });
  }

  if (routeAdd) {
    routeAdd.addEventListener("click", () => {
      syncRoutesFromDom();
      routes.push({ category: "", reviewer_id: null, reviewer_name: "" });
      renderRoutes();
      const last = routeHost.querySelector(
        ".lm-rv-route:last-child .lm-rv-route-cat");
      if (last) last.focus();
    });
  }

  if (routeHost) {
    routeHost.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const row = btn.closest(".lm-rv-route");
      if (!row) return;
      const i = Number(row.getAttribute("data-i"));
      syncRoutesFromDom();
      if (btn.classList.contains("lm-rv-route-del")) {
        routes.splice(i, 1);
      } else if (btn.classList.contains("lm-rv-route-up") && i > 0) {
        routes.splice(i - 1, 0, routes.splice(i, 1)[0]);
      } else if (btn.classList.contains("lm-rv-route-down")
        && i < routes.length - 1) {
        routes.splice(i + 1, 0, routes.splice(i, 1)[0]);
      } else {
        return;
      }
      renderRoutes();
    });
    routeHost.addEventListener("change", () => {
      syncRoutesFromDom();
      renderHint();
    });
    routeHost.addEventListener("input", (ev) => {
      if (!ev.target.classList.contains("lm-rv-route-cat")) return;
      syncRoutesFromDom();
      renderHint();
    });
  }

  async function load() {
    try {
      const data = await LMApi.projectDashboard(projectId);
      const policy = (data && data.review_policy) || committed;
      committed = {
        pass: !!policy.pass,
        untestable: !!policy.untestable,
        default_reviewer_id: (data && data.default_reviewer_id) || null,
        routes: (data && data.review_routes) || [],
      };
      await loadReviewers(committed.default_reviewer_id,
        data && data.default_reviewer_name);
      await loadCategories();
      paint(committed);
      // Only an admin gets the panel. A reader who can see the project can also
      // read this endpoint, so without this gate they would be shown working
      // checkboxes that fail only on save.
      if (data && data.can_edit_policy) { panel.hidden = false; }
    } catch (err) {
      // A reader hitting 403 is the expected path, not a fault: leave the
      // panel hidden and stay quiet. Anything else is worth surfacing.
      const status = err && err.status;
      if (status !== 403 && status !== 401) {
        panel.hidden = false;
        setMsg("评审策略加载失败：" + ((err && err.message) || "未知错误"), "error");
      }
    }
  }

  async function save() {
    syncRoutesFromDom();
    // Validated here as well as on the server so the user is told which rule is
    // wrong, instead of getting one generic 400 for the whole table.
    const cleaned = [];
    const seen = new Set();
    for (let i = 0; i < routes.length; i += 1) {
      const r = routes[i];
      const cat = String(r.category || "").trim();
      if (!cat && !r.reviewer_id) continue;   // untouched blank row: drop it
      if (!cat) {
        setMsg(`第 ${i + 1} 条规则未填写区分`, "error");
        return;
      }
      if (!r.reviewer_id) {
        setMsg(`区分「${cat}」未选择审核人`, "error");
        return;
      }
      const wild = cat.endsWith("*");
      const key = (normCat(wild ? cat.slice(0, -1) : cat) + (wild ? "*" : ""))
        .toLowerCase();
      if (seen.has(key)) {
        setMsg(`区分「${cat}」重复，后一条永远不会生效`, "error");
        return;
      }
      seen.add(key);
      cleaned.push({ category: cat, reviewer_id: Number(r.reviewer_id) });
    }

    const raw = reviewerSel ? reviewerSel.value : "";
    const next = {
      pass: passBox.checked,
      untestable: untBox.checked,
      // Sent as null (not omitted) when cleared, so "no reviewer" is an
      // instruction rather than an absent field the server would ignore.
      default_reviewer_id: raw ? Number(raw) : null,
      routes: cleaned,
    };
    saveBtn.disabled = true;
    setMsg("保存中…");
    try {
      const data = await LMApi.setReviewPolicy(projectId, next);
      // Repaint from the server's canonical answer: it normalises 区分 values
      // ("01" -> "1"), so showing the typed text would misrepresent what is
      // now stored.
      committed = { ...next, routes: (data && data.review_routes) || cleaned };
      paint(committed);
      setMsg("已保存");
      global.setTimeout(() => setMsg(""), 2500);
    } catch (err) {
      paint(committed);
      setMsg("保存失败：" + ((err && err.message) || "未知错误"), "error");
    } finally {
      saveBtn.disabled = false;
    }
  }

  saveBtn.addEventListener("click", save);
  (global.LMReady || Promise.resolve()).then(load);
})(window);
