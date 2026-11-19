const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../../app/static/js/lanmatrix/notifications.js"), "utf8",
);

function element(tagName) {
  return {
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
      const key = selector.slice("[data-".length, -1);
      if (Object.hasOwn(this.dataset, key)) return this;
      return this.parentElement?.closest(selector) ?? null;
    },
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };
}

async function openNotifications(notifications) {
  const bell = element("button");
  const list = element("div");
  const nodes = {
    "lm-notif": element("div"),
    "lm-notif-btn": bell,
    "lm-notif-list": list,
    "lm-notif-readall": element("button"),
  };
  const document = {
    hidden: true,
    listeners: {},
    createElement: element,
    getElementById: (id) => nodes[id] ?? null,
    querySelectorAll: () => [],
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };
  const calls = [];
  const LMApi = {
    meNotifications: async () => ({ notifications }),
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
