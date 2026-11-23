/* The suite report's [-]/[+] case controls used to be links whose onclick had
 * to return false. They are disclosure buttons now: native <button
 * type="button"> supplies Space/Enter activation, aria-expanded carries the
 * per-case state, aria-controls ties trigger to section, and the global
 * control names its next action because case states can be mixed.
 *
 * The markup test reads the Jinja source; the behaviour tests run the REAL
 * inline script from base.html against a minimal DOM so they cannot drift.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const base = fs.readFileSync(path.join(__dirname, "../../app/templates/reports/base.html"), "utf8");
const suite = fs.readFileSync(path.join(__dirname, "../../app/templates/reports/suite.html"), "utf8");
const script = base.match(/<script type="text\/javascript">([\s\S]*?)<\/script>/)[1];

function control(id, attrs, text) {
  return {
    id,
    attrs: { ...attrs },
    textContent: text,
    style: {},
    getAttribute(name) {
      return name in this.attrs ? this.attrs[name] : null;
    },
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    removeAttribute(name) {
      delete this.attrs[name];
    },
  };
}

function renderPage(caseCount, hash = "") {
  const global = control(
    "lnk_all",
    {
      "data-collapse": "all",
      "data-expanded": "true",
      "aria-expanded": "true",
      "aria-label": "收起全部用例详情",
    },
    "[-]",
  );
  const cases = [];
  const sections = [];
  for (let n = 1; n <= caseCount; n += 1) {
    cases.push(
      control(
        `lnk_case-${n}`,
        {
          "data-collapse": "case",
          "data-case": `case-${n}`,
          "aria-controls": `div_case-${n}`,
          "aria-expanded": "true",
          "aria-labelledby": `case-${n}`,
        },
        "[-]",
      ),
    );
    sections.push({ id: `div_case-${n}`, style: {} });
  }
  const byId = new Map([["lnk_all", global]]);
  for (const element of [...cases, ...sections]) byId.set(element.id, element);
  const listeners = {};
  const document = {
    getElementById: (id) => byId.get(id) || null,
    querySelectorAll: (selector) => (selector === 'button[data-collapse="case"]' ? cases.slice() : []),
  };
  const window = {
    location: { hash },
    addEventListener: (type, handler) => {
      listeners[type] = handler;
    },
  };
  const context = vm.createContext({ document, window });
  vm.runInContext(script, context);
  return { context, global, cases, sections, listeners };
}

test("report renders native disclosure buttons with names, state and controls", () => {
  assert.doesNotMatch(suite, /<a[^>]*data-collapse/);
  const globalTag = suite.match(/<button[^>]*id="lnk_all"[^>]*>/)[0];
  assert.match(globalTag, /type="button"/);
  assert.match(globalTag, /data-collapse="all"/);
  assert.match(globalTag, /aria-label="收起全部用例详情"/);
  // Before the load handler runs every case is open, so the aggregate state
  // is known at render time.
  assert.match(globalTag, /aria-expanded="true"/);
  assert.doesNotMatch(globalTag, /href=/);
  const caseTag = suite.match(/<button[^>]*id="lnk_\{\{ case\.anchor \}\}"[^>]*>/)[0];
  assert.match(caseTag, /type="button"/);
  assert.match(caseTag, /aria-controls="div_\{\{ case\.anchor \}\}"/);
  assert.match(caseTag, /aria-expanded="true"/);
  assert.match(caseTag, /aria-labelledby="\{\{ case\.anchor \}\}"/);
  assert.doesNotMatch(caseTag, /href=/);
  // Wiring: the global button toggles from its next-action state, a case
  // button expands when collapsed.
  assert.match(suite, /onclick="switchAll\('', this\.getAttribute\('data-expanded'\) !== 'true'\)"/);
  assert.match(
    suite,
    /onclick="switchAll\('\{\{ case\.anchor \}\}', this\.getAttribute\('aria-expanded'\) === 'false'\)"/,
  );
  // Real navigation affordances stay links.
  assert.match(suite, /<a href="#TestDetail">back<\/a>/);
  assert.match(
    suite,
    /<a href="#\{\{ case\.anchor \}\}" onclick="showTC\('\{\{ case\.anchor \}\}'\)">Case \{\{ case\.id \}\}<\/a>/,
  );
});

test("load keeps the reference collapsed default and keeps naming the next action", () => {
  const { listeners, global, cases, sections } = renderPage(2);
  listeners.load();
  for (const element of cases) {
    assert.equal(element.getAttribute("aria-expanded"), "false");
    assert.equal(element.textContent, "[+]");
  }
  for (const section of sections) assert.equal(section.style.display, "none");
  assert.equal(global.getAttribute("data-expanded"), "false");
  assert.equal(global.getAttribute("aria-label"), "展开全部用例详情");
  assert.equal(global.textContent, "[+]");
  // All cases are collapsed after load, so the aggregate state is false.
  assert.equal(global.getAttribute("aria-expanded"), "false");
});

test("global control expands then collapses every case with next-action naming", () => {
  const { context, listeners, global, cases, sections } = renderPage(2);
  listeners.load();
  const click = () => context.switchAll("", global.getAttribute("data-expanded") !== "true");
  click();
  for (const element of cases) {
    assert.equal(element.getAttribute("aria-expanded"), "true");
    assert.equal(element.textContent, "[-]");
  }
  for (const section of sections) assert.equal(section.style.display, "");
  assert.equal(global.getAttribute("aria-label"), "收起全部用例详情");
  assert.equal(global.textContent, "[-]");
  // A global action knows the exact resulting state.
  assert.equal(global.getAttribute("aria-expanded"), "true");
  click();
  for (const element of cases) assert.equal(element.getAttribute("aria-expanded"), "false");
  for (const section of sections) assert.equal(section.style.display, "none");
  assert.equal(global.getAttribute("aria-label"), "展开全部用例详情");
  assert.equal(global.getAttribute("aria-expanded"), "false");
});

test("a case control toggles only its own section and leaves the global label alone", () => {
  const { context, listeners, global, cases, sections } = renderPage(2);
  listeners.load();
  const click = (index) =>
    context.switchAll(`case-${index + 1}`, cases[index].getAttribute("aria-expanded") === "false");
  click(0);
  assert.equal(cases[0].getAttribute("aria-expanded"), "true");
  assert.equal(cases[0].textContent, "[-]");
  assert.equal(sections[0].style.display, "");
  assert.equal(cases[1].getAttribute("aria-expanded"), "false");
  assert.equal(sections[1].style.display, "none");
  assert.equal(global.getAttribute("aria-label"), "展开全部用例详情");
  // One open and one closed case is a mixed state: the global control
  // claims no aggregate expanded state rather than guessing.
  assert.equal(global.getAttribute("aria-expanded"), null);
  click(1);
  assert.equal(cases[1].getAttribute("aria-expanded"), "true");
  assert.equal(global.getAttribute("aria-expanded"), "true");
  click(0);
  assert.equal(cases[0].getAttribute("aria-expanded"), "false");
  assert.equal(sections[0].style.display, "none");
});

test("a #case-N fragment opens that case after the initial collapse", () => {
  const { listeners, global, cases, sections } = renderPage(2, "#case-2");
  listeners.load();
  assert.equal(cases[1].getAttribute("aria-expanded"), "true");
  assert.equal(cases[1].textContent, "[-]");
  assert.equal(sections[1].style.display, "");
  assert.equal(cases[0].getAttribute("aria-expanded"), "false");
  assert.equal(sections[0].style.display, "none");
  assert.equal(global.getAttribute("aria-label"), "展开全部用例详情");
  // The fragment opened one case, so the aggregate state is mixed.
  assert.equal(global.getAttribute("aria-expanded"), null);
});