const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../../app/static/js/lanmatrix/notifications.js"), "utf8",
);
const template = fs.readFileSync(
  path.join(__dirname, "../../app/templates/lanmatrix/base_lm.html"), "utf8",
);

function element(tagName) {
  const node = {
    tagName,
    dataset: {},
    attributes: {},
    children: [],
    listeners: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
    },
    append(...children) { children.forEach((child) => this.appendChild(child)); },
    replaceChildren(...children) {
      this.children = [];
      this.append(...children);
    },
    closest(selector) {
      if (selector.startsWith("[data-")) {
        const key = selector.slice("[data-".length, -1);
        if (Object.hasOwn(this.dataset, key)) return this;
      } else if (selector.startsWith(".")) {
        const cls = selector.slice(1);
        if ((this.className || "").split(/\s+/).includes(cls)) return this;
      }
      return this.parentElement?.closest(selector) ?? null;
    },
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };
  node.classList = {
    toggle(name, force) {
      const names = new Set((node.className || "").split(/\s+/).filter(Boolean));
      const on = force === undefined ? !names.has(name) : Boolean(force);
      if (on) names.add(name); else names.delete(name);
      node.className = [...names].join(" ");
      return on;
    },
  };
  return node;
}

function scopeButtons() {
  const unreadCount = element("span");
  unreadCount.dataset.count = "unread";
  const unread = element("button");
  unread.className = "lm-notif-tab is-active";
  unread.dataset.scope = "unread";
  unread.attributes["aria-pressed"] = "true";
  unread.append(unreadCount);

  const historyCount = element("span");
  historyCount.dataset.count = "history";
  const history = element("button");
  history.className = "lm-notif-tab";
  history.dataset.scope = "history";
  history.attributes["aria-pressed"] = "false";
  history.append(historyCount);

  const tabs = element("div");
  tabs.append(unread, history);
  return { tabs, unread, history, unreadCount, historyCount };
}

async function openNotifications(notifications, payload = {}) {
  const bell = element("button");
  const list = element("div");
  const foot = element("div");
  foot.hidden = true;
  const controls = scopeButtons();
  const nodes = {
    "lm-notif": element("div"),
    "lm-notif-btn": bell,
    "lm-notif-list": list,
    "lm-notif-readall": element("button"),
    "lm-notif-tabs": controls.tabs,
    "lm-notif-empty": element("div"),
    "lm-notif-foot": foot,
  };
  const document = {
    hidden: true,
    listeners: {},
    createElement: element,
    getElementById: (id) => nodes[id] ?? null,
    querySelectorAll: (selector) => {
      if (selector === "#lm-notif-tabs [data-count]") {
        return [controls.unreadCount, controls.historyCount];
      }
      if (selector === "#lm-notif-tabs .lm-notif-tab") {
        return [controls.unread, controls.history];
      }
      return [];
    },
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };
  const calls = [];
  const requests = [];
  const LMApi = {
    meNotifications: async (params) => {
      requests.push({ limit: params.limit, scope: params.scope });
      return {
        notifications,
        unread: payload.unread || 0,
        history: payload.history || 0,
      };
    },
    meNotificationsUnread: async () => ({ unread: 0 }),
    markNotificationsRead: async (ids) => { calls.push(["read", ...ids]); },
    archiveNotifications: async (ids) => { calls.push(["archive", ...ids]); },
  };
  vm.runInNewContext(source, {
    window: { LMApi }, document, LMApi, clearInterval() {},
  });
  document.listeners.DOMContentLoaded();
  bell.listeners.click({ stopPropagation() {} });
  await new Promise((resolve) => setImmediate(resolve));

  return {
    rows: list.children,
    calls,
    requests,
    tick: () => new Promise((resolve) => setImmediate(resolve)),
    ...controls,
    foot,
    click(target, modifiers = {}) {
      const event = {
        target,
        ...modifiers,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() {},
      };
      list.listeners.click(event);
      return event;
    },
    clickScope(target) {
      const event = {
        target,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() {},
      };
      controls.tabs.listeners.click(event);
      return event;
    },
  };
}

test("a linked unread row renders sibling link and archive controls", async () => {
  const url = "/lanmatrix/tasks/41";
  const { rows: [row] } = await openNotifications([{ id: 41, link_url: url }]);
  const [link, archive] = row.children;

  assert.equal(row.tagName, "div");
  assert.match(row.className, /\bunread\b/);
  assert.equal(link.tagName, "a");
  assert.equal(link.attributes.href, url);
  assert.equal(archive.tagName, "button");
  assert.equal(archive.type, "button");
  assert.ok(archive.attributes["aria-label"]);
});

test("delegated link clicks mark read and leave native navigation alone", async () => {
  const { rows: [row], click, calls } = await openNotifications([
    { id: 41, link_url: "/lanmatrix/tasks/41" },
  ]);
  const [link] = row.children;
  const [icon] = link.children;

  assert.equal(click(icon).defaultPrevented, false);
  assert.equal(click(icon, { ctrlKey: true }).defaultPrevented, false);
  assert.deepEqual(calls, [["read", 41], ["read", 41]]);
});

test("delegated archive clicks only archive", async () => {
  const { rows: [row], click, calls } = await openNotifications([
    { id: 41, link_url: "/lanmatrix/tasks/41" },
  ]);
  const [, archive] = row.children;

  click(archive);
  assert.deepEqual(calls, [["archive", 41]]);
});

test("unlinked content still marks read and archived rows have no archive control", async () => {
  const { rows: [unlinked, archived], click, calls } = await openNotifications([
    { id: 42 },
    { id: 43, link_url: "/lanmatrix/tasks/43", is_read: true, archived: true },
  ]);
  const [content, archive] = unlinked.children;

  assert.equal(content.tagName, "div");
  assert.equal(content.attributes.href, undefined);
  assert.equal(archive.tagName, "button");
  click(content.children[0]);
  assert.deepEqual(calls, [["read", 42]]);
  assert.doesNotMatch(archived.className, /\bunread\b/);
  assert.equal(archived.children.length, 1);
  assert.equal(archived.children[0].tagName, "a");
});

test("scope controls are a named group of native pressed buttons", () => {
  const [markup] = template.match(
    /<div class="lm-notif-tabs"[\s\S]*?<\/div>/,
  ) || [];

  assert.ok(markup, "notification scope markup is present");
  assert.match(markup, /role="group"/);
  assert.match(markup, /aria-label="通知范围"/);
  assert.doesNotMatch(markup, /role="tablist"|role="tab"|aria-selected/);
  assert.equal((markup.match(/type="button"/g) || []).length, 2);
  assert.match(
    markup,
    /id="lm-notif-tab-unread"[^>]*aria-pressed="true"/,
  );
  assert.match(
    markup,
    /id="lm-notif-tab-history"[^>]*aria-pressed="false"/,
  );
});

test("scope clicks sync pressed state, history footer and request scope", async () => {
  const {
    requests, tick, unread, history, unreadCount, historyCount, foot, clickScope,
  } = await openNotifications([{ id: 41 }], { unread: 1, history: 2 });

  assert.deepEqual(requests, [{ limit: 30, scope: "unread" }]);
  assert.equal(unreadCount.textContent, "(1)");
  assert.equal(historyCount.textContent, "(2)");
  assert.equal(unread.attributes["aria-pressed"], "true");
  assert.equal(history.attributes["aria-pressed"], "false");
  assert.match(unread.className, /\bis-active\b/);
  assert.equal(foot.hidden, true);

  const event = clickScope(historyCount);
  assert.equal(event.defaultPrevented, true);
  assert.equal(unread.attributes["aria-pressed"], "false");
  assert.equal(history.attributes["aria-pressed"], "true");
  assert.doesNotMatch(unread.className, /\bis-active\b/);
  assert.match(history.className, /\bis-active\b/);
  assert.equal(foot.hidden, false);
  await tick();
  assert.deepEqual(requests.at(-1), { limit: 30, scope: "history" });

  clickScope(history);
  await tick();
  assert.equal(requests.length, 2);

  clickScope(unreadCount);
  await tick();
  assert.equal(unread.attributes["aria-pressed"], "true");
  assert.equal(history.attributes["aria-pressed"], "false");
  assert.match(unread.className, /\bis-active\b/);
  assert.doesNotMatch(history.className, /\bis-active\b/);
  assert.equal(foot.hidden, true);
  assert.deepEqual(requests.at(-1), { limit: 30, scope: "unread" });
  assert.equal(requests.length, 3);
});
