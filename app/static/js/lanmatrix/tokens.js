/* Personal API tokens: issue (secret shown once), list and revoke. */
(function () {
  "use strict";
  var rows = document.getElementById("lm-token-rows");
  var info = document.getElementById("lm-token-info");
  var form = document.getElementById("lm-token-form");
  var secretBox = document.getElementById("lm-token-secret");
  var secretValue = document.getElementById("lm-token-value");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function when(iso) { return iso ? new Date(iso).toLocaleString() : "—"; }
  function state(token) {
    if (token.revoked_at) return "已吊销";
    if (token.expires_at && new Date(token.expires_at) <= new Date()) return "已过期";
    return "有效";
  }

  async function load() {
    try {
      var tokens = (await LMApi.meTokens()).tokens;
      info.textContent = tokens.length ? "共 " + tokens.length + " 个" : "还没有签发过 Token。";
      rows.innerHTML = tokens.map(function (t) {
        var live = state(t) === "有效";
        return "<tr><td>" + esc(t.name) + "</td><td><code>" + esc(t.prefix) + "…</code></td><td>" +
          esc(when(t.created_at)) + "</td><td>" + esc(when(t.expires_at)) + "</td><td>" +
          esc(when(t.last_used_at)) + "</td><td>" + state(t) + "</td><td>" +
          (live ? '<button type="button" class="btn" data-revoke="' + t.id + '" data-name="' +
            esc(t.name) + '">吊销</button>' : "") + "</td></tr>";
      }).join("") || '<tr><td colspan="7" class="lm-muted">—</td></tr>';
    } catch (e) {
      info.textContent = "加载失败：" + e.message;
    }
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    try {
      var data = await LMApi.createMeToken(form.name.value.trim(), Number(form.days.value));
      secretValue.textContent = data.token;
      secretBox.hidden = false;
      form.reset();
      form.days.value = 90;
      load();
    } catch (e) {
      LMUI.alert({ title: "签发失败", body: e.message });
    }
  });

  document.getElementById("lm-token-copy").addEventListener("click", function () {
    navigator.clipboard.writeText(secretValue.textContent).catch(function () {});
  });

  rows.addEventListener("click", async function (event) {
    var button = event.target.closest("[data-revoke]");
    if (!button) return;
    var ok = await LMUI.confirm({ title: "吊销 Token「" + button.dataset.name + "」？",
      body: "吊销后使用它的命令行和 Agent 会立即无法访问，不能恢复。", level: "danger", confirmText: "吊销" });
    if (!ok) return;
    try {
      await LMApi.revokeMeToken(Number(button.dataset.revoke));
      load();
    } catch (e) {
      LMUI.alert({ title: "吊销失败", body: e.message });
    }
  });

  load();
})();
