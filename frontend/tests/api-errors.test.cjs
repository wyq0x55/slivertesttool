const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function harness(response) {
  const calls = [];
  const context = vm.createContext({
    URLSearchParams, FormData,
    document: { addEventListener() {}, getElementById() { return null; } },
    async fetch(url, options) {
      calls.push({ url, options });
      if (url.endsWith("/auth/me")) {
        return { status: 200, text: async () => JSON.stringify({
          success: true, data: { user: {}, csrf_token: "initial" },
        }) };
      }
      return response;
    },
  });
  context.window = context;
  vm.runInContext(fs.readFileSync(path.resolve(__dirname,
    "../../app/static/js/lanmatrix/api.js"), "utf8"), context);
  return { api: context.LMApi, calls, ready: context.LMReady };
}

test("non JSON failures retain HTTP status and are distinguishable from an empty response", async () => {
  const { api, ready } = harness({ status: 502, text: async () => "<html>bad gateway</html>" });
  await ready;
  await assert.rejects(api.meOverview(), (error) =>
    error.code === "BAD_RESPONSE" && error.status === 502 && error.message.includes("502"));
});

test("JSON error envelopes preserve server reason and status", async () => {
  const { api, ready } = harness({ status: 409, text: async () => JSON.stringify({
    success: false, error: { code: "CONFLICT", message: "版本冲突", details: { version: 2 } },
  }) });
  await ready;
  await assert.rejects(api.meOverview(), (error) =>
    error.code === "CONFLICT" && error.message === "版本冲突" && error.details.version === 2);
});

test("password change uses the session CSRF and accepts its replacement", async () => {
  const { api, calls, ready } = harness({ status: 200, text: async () => JSON.stringify({
    success: true, data: { user: { must_change_password: false }, csrf_token: "rotated" },
  }) });
  await ready;
  const user = await api.changePassword("old-password", "new-password");
  assert.equal(user.must_change_password, false);
  assert.equal(calls.at(-1).url, "/api/v1/me/password");
  assert.equal(calls.at(-1).options.headers["X-CSRF-Token"], "initial");
  assert.equal(api.getToken(), "rotated");
});
