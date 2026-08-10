/* Review policy editor (project settings -> 成员管理).
 *
 * Two checkboxes decide which verdicts must be confirmed by a reviewer before
 * they count as accepted. The panel stays hidden until we know the current
 * user may change it: showing disabled controls to a reader advertises a
 * capability they do not have and invites a support question.
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

  // Remembers what the server last confirmed, so a failed save can put the
  // controls back instead of leaving them showing a state that was rejected.
  let committed = { pass: false, untestable: true, default_reviewer_id: null };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function paint(policy) {
    passBox.checked = !!policy.pass;
    untBox.checked = !!policy.untestable;
    if (reviewerSel) {
      reviewerSel.value = policy.default_reviewer_id
        ? String(policy.default_reviewer_id) : "";
    }
  }

  // The picker is limited to project members (plus whoever is currently set),
  // matching the server-side check. Offering non-members would produce a 400 on
  // save and read as a bug rather than as the rule it is.
  async function loadReviewers(currentId, currentName) {
    if (!reviewerSel) return;
    let members = [];
    try {
      const data = await LMApi.listMembers(projectId);
      members = (data && data.members) || [];
    } catch (err) {
      // Not fatal: the panel is still usable for the checkboxes, and the
      // current reviewer is preserved as an option below.
    }
    const opts = ['<option value="">（未设置 · 回退项目负责人）</option>'];
    const seen = new Set();
    members.forEach((m) => {
      if (!m.user_id || seen.has(m.user_id)) return;
      seen.add(m.user_id);
      const label = m.display_name || m.username || `#${m.user_id}`;
      opts.push(`<option value="${esc(m.user_id)}">${esc(label)}` +
        `${m.role ? `（${esc(m.role)}）` : ""}</option>`);
    });
    // Keep the reviewer that is actually configured even if they are no longer
    // in the member list, so opening the panel cannot silently clear them.
    if (currentId && !seen.has(Number(currentId))) {
      opts.push(`<option value="${esc(currentId)}">` +
        `${esc(currentName || `#${currentId}`)}（非成员）</option>`);
    }
    reviewerSel.innerHTML = opts.join("");
  }

  function setMsg(text, kind) {
    msg.textContent = text || "";
    msg.className = kind === "error" ? "lm-rv-pol-err" : "muted";
  }

  async function load() {
    try {
      const data = await LMApi.projectDashboard(projectId);
      const policy = (data && data.review_policy) || committed;
      committed = {
        pass: !!policy.pass,
        untestable: !!policy.untestable,
        default_reviewer_id: (data && data.default_reviewer_id) || null,
      };
      await loadReviewers(committed.default_reviewer_id,
        data && data.default_reviewer_name);
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
    const raw = reviewerSel ? reviewerSel.value : "";
    const next = {
      pass: passBox.checked,
      untestable: untBox.checked,
      // Sent as null (not omitted) when cleared, so "no reviewer" is an
      // instruction rather than an absent field the server would ignore.
      default_reviewer_id: raw ? Number(raw) : null,
    };
    saveBtn.disabled = true;
    setMsg("保存中…");
    try {
      await LMApi.setReviewPolicy(projectId, next);
      committed = next;
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
