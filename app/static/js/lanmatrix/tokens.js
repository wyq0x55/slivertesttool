/* Personal API tokens: issue (secret shown once), list and revoke. */
(function () {
  "use strict";
  var rows = document.getElementById("lm-token-rows");
  var info = document.getElementById("lm-token-info");
  var form = document.getElementById("lm-token-form");
  var secretBox = document.getElementById("lm-token-secret");
  // The banner starts empty in the template and is filled only after a token
  // is issued, so these elements do not exist yet.
  var secretValue = null;
  var copyBtn = null;
  var copyDefaultLabel = "复制";
  var copyTimer = null;

  function textVal(v) {
    return v == null ? "" : String(v);
  }

  function buildTokenRow(t) {
    var tr = document.createElement("tr");
    var nameTd = document.createElement("td");
    nameTd.textContent = textVal(t.name);

    var prefixTd = document.createElement("td");
    var code = document.createElement("code");
    code.textContent = textVal(t.prefix) + "…";
    prefixTd.appendChild(code);

    var createdTd = document.createElement("td");
    createdTd.textContent = when(t.created_at);

    var expiresTd = document.createElement("td");
    expiresTd.textContent = when(t.expires_at);

    var lastUsedTd = document.createElement("td");
    lastUsedTd.textContent = when(t.last_used_at);

    var tokenState = state(t);
    var stateTd = document.createElement("td");
    stateTd.textContent = tokenState;

    var actionTd = document.createElement("td");
    if (tokenState === "有效") {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn";
      btn.dataset.revoke = String(t.id);
      btn.dataset.name = textVal(t.name);
      btn.textContent = "吊销";
      actionTd.appendChild(btn);
    }
    tr.append(nameTd, prefixTd, createdTd, expiresTd, lastUsedTd, stateTd, actionTd);
    return tr;
  }

  function buildEmptyRow() {
    var tr = document.createElement("tr");
    var td = document.createElement("td");
    td.colSpan = 7;
    td.className = "lm-muted";
    td.textContent = "—";
    tr.appendChild(td);
    return tr;
  }

  function legacyCopy(text) {
    var ta = Object.assign(document.createElement("textarea"), { value: text });
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;inset-block-start:0;opacity:0";
    document.body.append(ta);
    ta.select();
    try {
      return Boolean(document.execCommand("copy"));
    } catch (e) {
      return false;
    } finally {
      ta.remove();
    }
  }

  // The banner is a mounted live region that is filled only after a token is
  // issued. Adding text to it is what gets announced; unhiding it would not be.
  // The secret is written with textContent so it is never parsed as markup.
  function showSecret(token) {
    var warningP = document.createElement("p");
    var strong = document.createElement("b");
    strong.textContent = "请立即复制保存，此 Token 只显示这一次。";
    warningP.appendChild(strong);
    warningP.appendChild(document.createTextNode("之后在命令行设置 "));
    var envCode = document.createElement("code");
    envCode.textContent = "SILVER_CLI_TOKEN";
    warningP.appendChild(envCode);
    warningP.appendChild(document.createTextNode(" 即可使用。"));

    var valueP = document.createElement("p");
    var valueCode = document.createElement("code");
    valueCode.id = "lm-token-value";
    valueCode.textContent = textVal(token);
    valueP.appendChild(valueCode);
    valueP.appendChild(document.createTextNode(" "));
    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "btn";
    copy.id = "lm-token-copy";
    copy.textContent = "复制";
    valueP.appendChild(copy);

    secretBox.replaceChildren(warningP, valueP);
    secretValue = document.getElementById("lm-token-value");
    copyBtn = document.getElementById("lm-token-copy");
    copyDefaultLabel = copyBtn ? copyBtn.textContent : "复制";
    resetCopyFeedback();
    if (copyBtn) copyBtn.addEventListener("click", copySecret);
  }

  function resetCopyFeedback() {
    if (!copyBtn) return;
    clearTimeout(copyTimer);
    copyTimer = null;
    copyBtn.removeAttribute("data-copied");
    copyBtn.removeAttribute("data-failed");
    copyBtn.textContent = copyDefaultLabel;
  }

  function showCopyFeedback(ok) {
    if (!copyBtn) return;
    resetCopyFeedback();
    copyBtn.setAttribute(ok ? "data-copied" : "data-failed", "");
    copyBtn.textContent = ok ? "已复制" : "复制失败";
    copyTimer = setTimeout(resetCopyFeedback, 1200);
  }

  async function copySecret() {
    var text = secretValue ? secretValue.textContent : "";
    if (!text) return;
    var ok = false;
    if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        ok = true;
      } catch (e) {
        ok = legacyCopy(text);
      }
    } else {
      ok = legacyCopy(text);
    }
    showCopyFeedback(ok);
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
      if (tokens.length) {
        rows.replaceChildren(...tokens.map(buildTokenRow));
      } else {
        rows.replaceChildren(buildEmptyRow());
      }
    } catch (e) {
      info.textContent = "加载失败：" + e.message;
    }
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    try {
      var data = await LMApi.createMeToken(form.name.value.trim(), Number(form.days.value));
      showSecret(data.token);
      form.reset();
      form.days.value = 90;
      load();
    } catch (e) {
      LMUI.alert({ title: "签发失败", body: e.message });
    }
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
