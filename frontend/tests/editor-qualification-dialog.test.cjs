const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function createElement(tagName) {
  const attributes = new Map();
  const listeners = new Map();
  const node = {
    tagName,
    id: "",
    value: "",
    textContent: "",
    children: [],
    style: {},
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    setAttribute(name, value) {
      attributes.set(name, value);
      this[name] = value;
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    click() {
      return listeners.get("click")?.();
    },
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };
  let markup = "";
  Object.defineProperty(node, "innerHTML", {
    get() {
      return markup;
    },
    set(value) {
      markup = value;
      // Register the elements the dialog markup declares, as a browser would, so
      // the module's getElementById lookups find them.
      for (const match of value.matchAll(/<([a-z0-9]+)\b([^>]*)>/g)) {
        const [, tag, rawAttributes] = match;
        const child = createElement(tag);
        const idMatch = /\bid="([^"]+)"/.exec(rawAttributes);
        if (idMatch) child.id = idMatch[1];
        for (const attribute of rawAttributes.matchAll(/([\w-]+)="([^"]*)"/g)) {
          if (attribute[1] !== "id") child.setAttribute(attribute[1], attribute[2]);
        }
        node.appendChild(child);
      }
      const text = (id) => {
        const pattern = new RegExp(`<([a-z0-9]+)[^>]*id="${id}"[^>]*>([\\s\\S]*?)</\\1>`);
        const found = pattern.exec(value);
        return found ? found[2].replace(/\s+/g, " ").trim() : "";
      };
      for (const child of node.children) {
        if (!child.id) continue;
        if (child.tagName === "h3" || child.tagName === "p" || child.tagName === "label") {
          child.textContent = text(child.id) || child.id;
        }
      }
      const labelMarkup = /<label\b[^>]*>([\s\S]*?)<\/label>/.exec(value);
      const labelNode = node.children.find((child) => child.tagName === "label");
      if (labelNode && labelMarkup) {
        labelNode.textContent = labelMarkup[1].replace(/\s+/g, " ").trim();
      }
    },
  });
  return node;
}

function findById(element, id) {
  if (element.id === id) return element;
  for (const child of element.children) {
    const found = findById(child, id);
    if (found) return found;
  }
  return null;
}

test("执行配置 opens a named signal list dialog with a labeled, described textarea", async () => {
  const body = createElement("body");
  const page = createElement("div");
  page.id = "lm-exec-config";
  page.dataset = { projectId: "42" };
  body.appendChild(page);
  // The button is page markup now; the module only binds it.
  const trigger = createElement("button");
  trigger.id = "lm-signal-list-btn";
  page.appendChild(trigger);

  const document = {
    readyState: "complete",
    body,
    createElement,
    getElementById: (id) => findById(body, id),
    querySelector: (selector) =>
      selector === ".lm-exec-config" || selector === ".lm-exec-config, .lm-editor" ? page : null,
    createTextNode: (text) => ({ textContent: text }),
  };
  const requests = [];
  const window = {
    LMApi: {
      async getProject(id) {
        requests.push(["getProject", id]);
        return { project: { default_signal_list: "v_speed" } };
      },
      async patchProject(id, updates) {
        requests.push(["patchProject", id, { ...updates }]);
      },
    },
  };
  const script = path.join(__dirname, "../../app/static/js/lanmatrix/editor_qualification.js");
  vm.runInNewContext(fs.readFileSync(script, "utf8"), { window, document });

  assert.equal(document.getElementById("lm-signal-list-dialog"), null, "built on demand, not at load");
  await trigger.click();
  await new Promise(setImmediate);

  const dialog = document.getElementById("lm-signal-list-dialog");
  assert.equal(dialog.tagName, "dialog");
  assert.equal(dialog.open, true);
  const heading = document.getElementById("lm-signal-list-title");
  assert.match(heading.textContent, /默认信号集配置/);
  assert.equal(dialog.getAttribute("aria-labelledby"), heading.id);

  const textarea = document.getElementById("lm-default-signal-list");
  assert.equal(textarea.tagName, "textarea");
  assert.equal(textarea.value, "v_speed");
  const label = dialog.children.find((child) => child.tagName === "label");
  assert.ok(label, "a native textarea label is present");
  assert.equal(label.getAttribute("for"), textarea.id);
  assert.match(label.textContent, /观测信号/);

  const described = document.getElementById(textarea.getAttribute("aria-describedby"));
  assert.equal(described.tagName, "p");
  assert.match(described.textContent, /每行填写一个观测信号名/);

  document.getElementById("lm-signal-list-cancel").click();
  assert.equal(dialog.open, false);
  assert.deepEqual(requests, [["getProject", 42]]);

  await trigger.click();
  await new Promise(setImmediate);
  assert.equal(document.getElementById("lm-signal-list-dialog"), dialog);
  assert.equal(dialog.open, true);
  textarea.value = " v_speed \r\n\r\n u1_brake_lamp ";
  await document.getElementById("lm-signal-list-save").click();
  assert.deepEqual(requests, [
    ["getProject", 42],
    ["getProject", 42],
    ["patchProject", 42, { default_signal_list: "v_speed\nu1_brake_lamp" }],
  ]);
  assert.equal(dialog.open, false);
});