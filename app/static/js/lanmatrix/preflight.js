/* preflight.js — the submit-form readiness bar on the project tasks page.
 *
 * Why this exists: every precondition for submitting a run used to be invisible
 * until *after* the user had picked a model, scanned a folder, ticked test ids
 * and pressed 提交. The "no model registered" case in particular only surfaced
 * at that last click (project_tasks.js), by which point the work is done and the
 * fix needs a different person (a system administrator). Preconditions belong
 * at the top of the form, before the effort, not behind the submit button.
 *
 * What is deliberately NOT a check here: "你是项目成员". Every project role
 * (project_admin/editor/reviewer/reader) carries `task.upload`, and a system
 * admin is granted project_admin implicitly, so on this page that condition is
 * a tautology -- the page does not load otherwise. A tick that can never turn
 * red is decoration, and decoration mixed in with real checks devalues them.
 * The role is shown as plain information instead, clearly not a pass/fail row.
 */
(function () {
  "use strict";

  /* Levels, worst first. `blocked` means submit cannot succeed; `warn` means it
   * can succeed and then fail later (or behave surprisingly); `info` is context. */
  var ORDER = { blocked: 0, warn: 1, ok: 2, info: 3 };

  var ROLE_ZH = {
    project_admin: "项目管理员",
    editor: "编辑者",
    reviewer: "评审者",
    reader: "只读成员"
  };

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Top-level module names imported by a judge.
   *
   * The server does this with a real AST walk. This is a regex, and the gap is
   * accepted knowingly: it is anchored to the start of a line so that `import`
   * appearing inside a string or a comment body is not picked up, and the only
   * realistic false positive left is an import written at column 0 inside a
   * triple-quoted block. That is why an unresolved module is reported as a
   * caution and never blocks submission -- a wrong warning must stay cheap.
   *
   * Relative imports (`from . import x`) are skipped: they resolve against the
   * case folder itself, which is uploaded by definition.
   */
  function scanImports(text) {
    var out = [];
    var re = /^[ \t]*(?:from[ \t]+([A-Za-z_][\w.]*)[ \t]+import[ \t]|import[ \t]+([A-Za-z_][\w.]*))/gm;
    var m;
    while ((m = re.exec(String(text || "")))) {
      var name = m[1] || m[2];
      if (name) out.push(name.split(".")[0]);
    }
    return out;
  }

  /* Module names a judge imports that nothing in the upload can satisfy.
   *
   * `paths` is every relative path the browser is about to send (case folder +
   * lib + stdlib). A module is considered resolvable if any path ends in
   * `<name>.py` or looks like the package directory `<name>/__init__.py`.
   */
  function unresolvedImports(texts, paths, stdlib, silverRoots) {
    var known = {};
    (stdlib || []).forEach(function (n) { known[n] = 1; });
    (silverRoots || []).forEach(function (n) { known[n] = 1; });

    var supplied = {};
    (paths || []).forEach(function (p) {
      var parts = String(p || "").split("/");
      var base = parts[parts.length - 1] || "";
      if (base === "__init__.py" && parts.length >= 2) {
        supplied[parts[parts.length - 2]] = 1;
      } else if (/\.py$/.test(base)) {
        supplied[base.slice(0, -3)] = 1;
      }
      // A bare directory name can also satisfy a package import when the
      // folder was uploaded without an __init__.py (namespace package).
      parts.slice(0, -1).forEach(function (d) { if (d) supplied[d] = 1; });
    });

    var missing = {};
    (texts || []).forEach(function (t) {
      scanImports(t).forEach(function (name) {
        if (known[name] || supplied[name]) return;
        missing[name] = 1;
      });
    });
    return Object.keys(missing).sort();
  }

  /* Turn the form's current state into the rows to display.
   *
   * Pure on purpose: this is the part that decides what the user is told, so it
   * is the part that has to be testable without a browser, a server or a
   * database. Callers pass a plain object; nothing here touches the DOM.
   */
  function evaluate(s) {
    s = s || {};
    var rows = [];
    var models = s.models || [];

    // 1. Model. The one that used to be discovered only at the submit click.
    if (!models.length) {
      rows.push({
        key: "model", level: "blocked", label: "模型",
        text: "尚未注册 .sil 模型，无法提交",
        hint: s.adminHint || "请联系系统管理员在管理台注册 .sil 模型。"
      });
    } else {
      var chosen = null;
      for (var i = 0; i < models.length; i++) {
        if (models[i] && models[i].name === s.selectedModel) { chosen = models[i]; break; }
      }
      if (!chosen) {
        for (var j = 0; j < models.length; j++) {
          if (models[j] && models[j].is_current) { chosen = models[j]; break; }
        }
      }
      if (chosen && chosen.exists === false) {
        // Registered but the file is gone from the server: the upload succeeds
        // and the run then fails. Worth a hard stop, not a footnote in a
        // <option> label where it is easy to miss.
        rows.push({
          key: "model", level: "blocked", label: "模型",
          text: "模型「" + chosen.name + "」在服务器上缺失",
          hint: "该 .sil 已登记但文件不在服务器上，提交后会运行失败。请改选其他模型或联系管理员。"
        });
      } else {
        rows.push({
          key: "model", level: "ok", label: "模型",
          text: chosen ? ("已选择 " + chosen.name) : (models.length + " 个模型可用")
        });
      }
    }

    // 2. Test-case folder.
    if (!s.folderChosen) {
      rows.push({
        key: "folder", level: "blocked", label: "测试用例",
        text: "尚未选择测试用例文件夹",
        hint: "选择包含 judge.py 子文件夹的目录。"
      });
    } else if (!s.testIdCount) {
      rows.push({
        key: "folder", level: "blocked", label: "测试用例",
        text: "所选文件夹内没有 judge.py",
        hint: "每个含 judge.py 的子文件夹才算一个 test id，请确认选对了目录。"
      });
    } else if (!s.selectedCount) {
      rows.push({
        key: "folder", level: "warn", label: "测试用例",
        text: "发现 " + s.testIdCount + " 个 test id，但一个都没勾选",
        hint: "勾选要运行的 test id 后才能提交。"
      });
    } else {
      rows.push({
        key: "folder", level: "ok", label: "测试用例",
        text: "已勾选 " + s.selectedCount + " / " + s.testIdCount + " 个 test id"
      });
    }

    // 3. lib. Only spoken about when it actually matters -- an unconditional
    // "未选择 lib 文件夹" warning on an optional field is crying wolf, and the
    // next real warning gets ignored with it.
    var miss = s.missingModules || [];
    if (miss.length) {
      rows.push({
        key: "lib", level: "warn", label: "lib 依赖",
        text: "judge.py 引用了未随包上传的模块：" + miss.join("、"),
        hint: "这些模块在测试用例、lib、stdlib 文件夹里都找不到，任务会入队但运行时报 ImportError。请补选包含它们的 lib 文件夹。"
      });
    } else if (s.scanned) {
      rows.push({
        key: "lib", level: "ok", label: "lib 依赖",
        text: s.libChosen ? "已选择 lib 文件夹，依赖齐全" : "judge.py 未引用需要额外上传的模块"
      });
    }

    // 4. Licence. Not a blocker -- the job queues either way -- but "queued
    // forever because there are zero licences" is indistinguishable from
    // "queued normally" once you have pressed submit.
    var lic = s.license;
    if (lic && !lic.total) {
      rows.push({
        key: "license", level: "warn", label: "授权",
        text: "当前没有可用授权，提交后任务会一直排队",
        hint: "请联系系统管理员配置 Silver 授权数量。"
      });
    } else if (lic && lic.total && !lic.available && lic.queued_jobs) {
      rows.push({
        key: "license", level: "warn", label: "授权",
        text: "授权已全部占用，前面还有 " + lic.queued_jobs + " 个任务排队"
      });
    }

    // 5. Role. Information, not a check -- see the file header.
    if (s.role) {
      rows.push({
        key: "role", level: "info", label: "你的角色",
        text: ROLE_ZH[s.role] || s.role
      });
    }

    rows.sort(function (a, b) { return ORDER[a.level] - ORDER[b.level]; });
    return rows;
  }

  /** True when any row would prevent a successful submit. */
  function blocked(rows) {
    return (rows || []).some(function (r) { return r.level === "blocked"; });
  }

  /** One-line summary for the bar's header. */
  function summarize(rows) {
    rows = rows || [];
    var b = rows.filter(function (r) { return r.level === "blocked"; }).length;
    var w = rows.filter(function (r) { return r.level === "warn"; }).length;
    // Deliberately not "无法提交": one blocked case (a registered .sil that is
    // missing on the server) does submit successfully and then fails at run
    // time. "现在提交会失败" is true of all of them.
    if (b) return b + " 项待解决，现在提交会失败";
    if (w) return w + " 项提醒，可以提交";
    return "可以提交";
  }

  function render(el, rows) {
    if (!el) return;
    rows = rows || [];
    if (!rows.length) { el.innerHTML = ""; el.hidden = true; return; }
    el.hidden = false;
    el.className = "lm-preflight" + (blocked(rows) ? " is-blocked" : "");
    var head = '<div class="lm-preflight-head">提交前置检查 · ' +
      esc(summarize(rows)) + "</div>";
    var body = rows.map(function (r) {
      return '<div class="lm-preflight-row lm-pf-' + esc(r.level) + '">' +
        '<span class="lm-pf-mark" aria-hidden="true">' +
        (r.level === "ok" ? "✓" : r.level === "info" ? "·" : "!") + "</span>" +
        '<span class="lm-pf-label">' + esc(r.label) + "</span>" +
        '<span class="lm-pf-text">' + esc(r.text) +
        (r.hint ? '<span class="lm-pf-hint">' + esc(r.hint) + "</span>" : "") +
        "</span></div>";
    }).join("");
    el.innerHTML = head + '<div class="lm-preflight-rows">' + body + "</div>";
  }

  window.LMPreflight = {
    evaluate: evaluate,
    render: render,
    blocked: blocked,
    summarize: summarize,
    scanImports: scanImports,
    unresolvedImports: unresolvedImports,
    ROLE_ZH: ROLE_ZH
  };
})();
