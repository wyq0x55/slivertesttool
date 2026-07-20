/* LAN Test Matrix API client — unified envelope + CSRF double-submit token.
 * All calls go through /api/v1 and share one session cookie. The CSRF token is
 * captured at login / bootstrap and echoed via the X-CSRF-Token header on every
 * state-changing request (server verifies with constant-time compare). */
(function (global) {
  "use strict";
  const BASE = "/api/v1";
  let csrfToken = null;

  class ApiError extends Error {
    constructor(code, message, details, status) {
      super(message || code);
      this.code = code;
      this.details = details;
      this.status = status;
    }
  }

  async function request(method, path, { body, raw, query } = {}) {
    const headers = {};
    let url = BASE + path;
    if (query) {
      const qs = new URLSearchParams(query).toString();
      if (qs) url += (url.includes("?") ? "&" : "?") + qs;
    }
    const opts = { method, headers, credentials: "same-origin" };
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = csrfToken || "";
    }
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (raw) {
      if (!resp.ok) throw new ApiError("HTTP_" + resp.status, "请求失败", null, resp.status);
      return resp;
    }
    let payload = null;
    try { payload = await resp.json(); } catch (e) { payload = null; }
    if (!payload) throw new ApiError("BAD_RESPONSE", "服务器无响应", null, resp.status);
    if (!payload.success) {
      const err = payload.error || {};
      throw new ApiError(err.code || "ERROR", err.message || "操作失败", err.details, resp.status);
    }
    return payload.data;
  }

  const LMApi = {
    ApiError,
    setToken(t) { csrfToken = t; },
    getToken() { return csrfToken; },

    async login(username, password) {
      const data = await request("POST", "/auth/login", { body: { username, password } });
      csrfToken = data.csrf_token;
      return data.user;
    },
    async register(payload) {
      const data = await request("POST", "/auth/register", { body: payload });
      if (data && data.csrf_token) csrfToken = data.csrf_token;
      return data;
    },
    async logout() {
      try { await request("POST", "/auth/logout", {}); } catch (e) { /* ignore */ }
      csrfToken = null;
    },
    async me() {
      const data = await request("GET", "/auth/me");
      csrfToken = data.csrf_token;
      return data.user;
    },

    listProjects() { return request("GET", "/projects"); },
    createProject(payload) { return request("POST", "/projects", { body: payload }); },
    getProject(id) { return request("GET", `/projects/${id}`); },
    patchProject(id, changes) { return request("PATCH", `/projects/${id}`, { body: { changes } }); },
    deleteProject(id) { return request("DELETE", `/projects/${id}`); },

    listFields(id, params) { return request("GET", `/projects/${id}/fields`, { query: params }); },
    addField(id, payload) { return request("POST", `/projects/${id}/fields`, { body: payload }); },
    patchField(id, fid, changes) { return request("PATCH", `/projects/${id}/fields/${fid}`, { body: { changes } }); },
    deleteField(id, fid) { return request("DELETE", `/projects/${id}/fields/${fid}`); },

    listItems(id, params) { return request("GET", `/projects/${id}/items`, { query: params }); },
    // opts: { anchor_id, place: "above"|"below" } for positional insert.
    createItem(id, values, opts) {
      const body = Object.assign({ values }, opts || {});
      return request("POST", `/projects/${id}/items`, { body });
    },
    patchItem(id, iid, version, changes) {
      return request("PATCH", `/projects/${id}/items/${iid}`, { body: { version, changes } });
    },
    deleteItem(id, iid) { return request("DELETE", `/projects/${id}/items/${iid}`); },
    duplicateItem(id, iid) { return request("POST", `/projects/${id}/items/${iid}/duplicate`, {}); },
    restoreItem(id, iid) { return request("POST", `/projects/${id}/items/${iid}/restore`, {}); },

    bulkDeleteItems(id, ids) { return request("POST", `/projects/${id}/items/bulk-delete`, { body: { ids } }); },
    bulkDuplicateItems(id, ids) { return request("POST", `/projects/${id}/items/bulk-duplicate`, { body: { ids } }); },
    moveItems(id, ids, direction) { return request("POST", `/projects/${id}/items/move`, { body: { ids, direction } }); },

    // Real-time collaboration: mint a short-lived token for the Yjs WebSocket
    // server. Returns { token, room, ws_url, expires_in }. Requires item.edit.
    getCollabToken(id) { return request("POST", `/projects/${id}/collab-token`, {}); },

    batchPreview(id, payload) { return request("POST", `/projects/${id}/items/batch-preview`, { body: payload }); },
    batchUpdate(id, payload) { return request("POST", `/projects/${id}/items/batch-update`, { body: payload }); },
    batchUndo(id, batchId) { return request("POST", `/projects/${id}/items/batch-undo`, { body: { batch_id: batchId } }); },

    listComments(id, iid) { return request("GET", `/projects/${id}/items/${iid}/comments`); },
    addComment(id, iid, field_key, content) {
      return request("POST", `/projects/${id}/items/${iid}/comments`, { body: { field_key, content } });
    },

    templateUrl(id) { return `${BASE}/projects/${id}/excel/template`; },
    createImport(id, file, mode) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("mode", mode || "upsert");
      return request("POST", `/projects/${id}/imports`, { body: fd });
    },
    commitImport(jobId) { return request("POST", `/imports/${jobId}/commit`, {}); },
    importTestMatrix(id, file, mode) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("mode", mode || "upsert");
      return request("POST", `/projects/${id}/testmatrix/import`, { body: fd });
    },
    testMatrixExportUrl(id) { return `${BASE}/projects/${id}/testmatrix/export`; },
    importLibFunc(id, file, mode) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("mode", mode || "upsert");
      return request("POST", `/projects/${id}/libfunc/import`, { body: fd });
    },
    importConst(id, file, mode) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("mode", mode || "upsert");
      return request("POST", `/projects/${id}/const/import`, { body: fd });
    },
    async exportProject(id, payload) {
      const resp = await request("POST", `/projects/${id}/exports`, { body: payload || {}, raw: true });
      return resp.blob();
    },

    listAudit(id, params) { return request("GET", `/projects/${id}/audit-logs`, { query: params }); },
    health() { return request("GET", "/health"); },

    dbOverview() { return request("GET", "/admin/db/overview"); },
    dbQuery(sql, readOnly) { return request("POST", "/admin/db/query", { body: { sql, read_only: readOnly } }); },
    dbTables() { return request("GET", "/admin/db/tables"); },
    dbTableSchema(table) { return request("GET", `/admin/db/tables/${encodeURIComponent(table)}/schema`); },
    dbTableRows(table, params) { return request("GET", `/admin/db/tables/${encodeURIComponent(table)}/rows`, { query: params }); },
    dbInsertRow(table, values) { return request("POST", `/admin/db/tables/${encodeURIComponent(table)}/rows`, { body: { values } }); },
    dbUpdateRow(table, pk, changes) { return request("PATCH", `/admin/db/tables/${encodeURIComponent(table)}/rows`, { body: { pk, changes } }); },
    dbDeleteRow(table, pk) { return request("DELETE", `/admin/db/tables/${encodeURIComponent(table)}/rows`, { body: { pk } }); },

    // --- Project membership ---------------------------------------------- //
    listMembers(id) { return request("GET", `/projects/${id}/members`); },
    memberCandidates(id, q) { return request("GET", `/projects/${id}/members/candidates`, { query: { q: q || "" } }); },
    addMember(id, payload) { return request("POST", `/projects/${id}/members`, { body: payload }); },
    patchMember(id, mid, role) { return request("PATCH", `/projects/${id}/members/${mid}`, { body: { role } }); },
    removeMember(id, mid) { return request("DELETE", `/projects/${id}/members/${mid}`); },

    // --- Project Upload Tasks (test execution) --------------------------- //
    listProjectTasks(id) { return request("GET", `/projects/${id}/tasks`); },
    uploadProjectTree(id, formData) { return request("POST", `/projects/${id}/tasks/upload-tree`, { body: formData }); },
    runSelectedTasks(id, payload) { return request("POST", `/projects/${id}/tasks/run-selected`, { body: payload || {} }); },
    projectTaskStatus(id, key) { return request("GET", `/projects/${id}/tasks/${key}`); },
    projectTaskDetail(id, key) { return request("GET", `/projects/${id}/tasks/${key}/detail`); },
    projectTaskStreamUrl(id, key) { return `${BASE}/projects/${id}/tasks/${key}/stream`; },
    projectTaskJdgrslt(id, key) { return request("GET", `/projects/${id}/tasks/${key}/jdgrslt`); },
    cancelProjectTask(id, key) { return request("POST", `/projects/${id}/tasks/${key}/cancel`, {}); },
    deleteProjectTask(id, key) { return request("DELETE", `/projects/${id}/tasks/${key}`); },
    deleteProjectTasksBatch(id, keys) { return request("POST", `/projects/${id}/tasks/delete_batch`, { body: { keys } }); },
    projectTaskDownloadUrl(id, key) { return `${BASE}/projects/${id}/tasks/${key}/download`; },
    projectTasksDownloadBatchUrl(id, keys) {
      return `${BASE}/projects/${id}/tasks/download_batch?keys=${encodeURIComponent(keys.join(","))}`;
    },

    // --- System-admin console -------------------------------------------- //
    adminListUsers() { return request("GET", "/admin/users"); },
    adminCreateUser(payload) { return request("POST", "/admin/users", { body: payload }); },
    adminUpdateUser(id, changes) { return request("PATCH", `/admin/users/${id}`, { body: { changes } }); },
    adminDeleteUser(id) { return request("DELETE", `/admin/users/${id}`); },
    adminGetModels() { return request("GET", "/admin/models"); },
    adminAddModel(name, path) { return request("POST", "/admin/models", { body: { name, path } }); },
    adminBulkModels(models) { return request("POST", "/admin/models/bulk", { body: { models } }); },
    adminRemoveModel(name) { return request("DELETE", "/admin/models", { body: { name } }); },
    adminGetLicense() { return request("GET", "/admin/license"); },
    adminSetLicense(count) { return request("POST", "/admin/license", { body: { count } }); },
    adminListTasks() { return request("GET", "/admin/tasks"); },
    adminCancelTask(key) { return request("POST", `/admin/tasks/${key}/cancel`, {}); },
    adminDeleteTask(key) { return request("DELETE", `/admin/tasks/${key}`); },
  };

  global.LMApi = LMApi;

  // Auto-bootstrap the CSRF token from the session if a page loads while logged in.
  global.LMReady = LMApi.me().then((u) => u).catch(() => null);

  document.addEventListener("click", async (e) => {
    if (e.target && e.target.id === "lm-logout") {
      e.preventDefault();
      await LMApi.logout();
      window.location = LM.urls.login;
    }
  });
})(window);
