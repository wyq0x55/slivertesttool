/* LAN Test Matrix — real-time collaboration controller.
 *
 * A build-free companion to the vendored Yjs runtime bundle
 * (app/static/vendor/collab/collab.umd.js, which assigns
 * `window.LMCollab = { Y, WebsocketProvider, Awareness }`). This file owns ALL
 * the CRDT session logic; editor.js only calls a handful of methods.
 *
 * Contract with the server (app/collab/doc_model.py):
 *   - top-level shared type per sheet, named `rows:test` / `rows:const` /
 *     `rows:lib`, each a Y.Array of Y.Map;
 *   - each Y.Map = the row's field values + `uuid` (CRDT identity) + `id`
 *     (server primary key). `row_order` is implicit = array index; a brand-new
 *     client row has NO `id` until the server materializer writes it back.
 *
 * Design: coarse, data-push binding. editor.js derives its plain item[] from the
 * Y.Arrays (getItems) and pushes them into whichever grid engine it runs; every
 * local mutation goes through the Y.Doc (setCell/insertRow/…); any remote change
 * fires onChange so editor re-renders. This needs zero grid.js/adapter changes.
 *
 * Everything here is strictly additive and non-breaking: if the bundle is
 * missing, the token endpoint denies the user, or the socket never syncs,
 * start() resolves to `false` and the editor stays in classic REST + polling.
 */
(function (global) {
  "use strict";

  const SKIP_ON_CLONE = { id: 1, uuid: 1, version: 1, row_order: 1, updated_at: 1 };
  const SKIP_ON_COPY = { row_order: 1, updated_at: 1 };
  const CONNECT_TIMEOUT_MS = 6000;
  // FIX: 只对远端变更做防抖渲染。本地编辑已经在 Univer 里显示了（用户
  // 刚打的字），80 ms 对远端更新足够流畅，又不会淹没渲染队列。
  const REMOTE_CHANGE_DEBOUNCE_MS = 80;
  // Silent token renewal (design: Phase 2「token 到期前静默续签」). The room token
  // is short-lived (server default ~120s) and is only checked at connect time, so
  // an already-open socket survives past expiry — but any auto-reconnect after a
  // network blip would present a stale token and be rejected. We refresh the
  // token this far (ms) BEFORE it expires and write it back into the provider's
  // ``params`` so every future (re)connect carries a valid one.
  const TOKEN_RENEW_SKEW_MS = 45000;   // renew this long before expiry
  const TOKEN_RENEW_MIN_MS = 20000;    // never schedule sooner than this
  const TOKEN_RENEW_RETRY_MS = 15000;  // retry cadence after a failed refresh

  function genUuid() {
    try {
      if (global.crypto && typeof global.crypto.randomUUID === "function") {
        return global.crypto.randomUUID();
      }
    } catch (_e) { /* fall through */ }
    // RFC-4122 v4 fallback (used when crypto.randomUUID is unavailable, e.g. an
    // insecure-origin LAN deployment served over plain HTTP).
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      const v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  // Stable, collision-resistant NEGATIVE placeholder id for a row the server has
  // not assigned a real primary key to yet. Derived from the uuid so every
  // client renders the same temp id for the same row (the grid keys rows by
  // numeric id), and it flips to the real positive id once the server writes it
  // back into the Y.Map. Never returns 0.
  function tempIdFromUuid(uuid) {
    const s = String(uuid || "");
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    const n = (h % 2000000000) + 1;
    return -n;
  }

  // Build a base ws:// origin (no path) from the optional server-provided URL,
  // falling back to the page host on port 1234. Rewrites a loopback host to the
  // page host (so a LAN client doesn't dial its own localhost) and forces wss on
  // an https page to avoid mixed-content blocking.
  function resolveWsBase(wsUrl) {
    const loc = global.location;
    const secure = loc.protocol === "https:";
    if (wsUrl) {
      try {
        const u = new URL(wsUrl, loc.href);
        if ((u.hostname === "localhost" || u.hostname === "127.0.0.1") &&
            loc.hostname && loc.hostname !== u.hostname) {
          u.hostname = loc.hostname;
        }
        if (secure) u.protocol = "wss:";
        return u.origin.replace(/\/+$/, "");
      } catch (_e) { /* fall through to derived default */ }
    }
    return (secure ? "wss:" : "ws:") + "//" + loc.hostname + ":1234";
  }

  const PALETTE = ["#2f80ed", "#eb5757", "#27ae60", "#f2994a", "#9b51e0",
                   "#00b8d9", "#e91e63", "#795548"];

  // ------------------------------------------------------------------ //
  // Steps sub-structure CRDT (item 3).
  //
  // The step-detail field ("steps" on the test sheet, "lib_stb" on lib) used to
  // be an opaque JSON *string* value on the row Y.Map — so any edit rewrote the
  // whole blob (last-writer-wins) and peers couldn't merge concurrent step edits.
  // We upgrade it to a nested CRDT: a Y.Map holding { input_signals,
  // expected_signals } (plain arrays; whole-value replace, rarely concurrent) and
  // a Y.Array of step Y.Maps (per-step, per-field granular merge).
  //
  // CRUCIAL boundary: everything OUTSIDE the live Y.Doc still sees the SAME JSON
  // string. getItems() re-serialises the nested Y back to a string, and the
  // server's snapshot_sheet stringifies it before the DB — so the execution-JSON
  // exporter and the Excel import/export keep working unchanged. A kill-switch
  // (window.LM_STEPS_CRDT === false) falls back to the legacy string blob.
  const STEP_FIELDS = { steps: 1, lib_stb: 1 };
  const STEP_SCALARS = ["no", "purpose", "operation", "subroutine", "args", "timing"];

  function stepsCrdtEnabled() { return global.LM_STEPS_CRDT !== false; }

  function normStepDoc(d) {
    d = d || {};
    return {
      input_signals: Array.isArray(d.input_signals) ? d.input_signals : [],
      expected_signals: Array.isArray(d.expected_signals) ? d.expected_signals : [],
      steps: Array.isArray(d.steps) ? d.steps : [],
    };
  }

  // Coerce ANY stored representation (JSON string | plain object | Y.Map) into a
  // plain StepDoc. Tolerant on purpose so a half-migrated room never throws.
  function stepsPlainFrom(v) {
    if (v == null || v === "") return normStepDoc(null);
    if (typeof v === "string") {
      try { return normStepDoc(JSON.parse(v)); } catch (_e) { return normStepDoc(null); }
    }
    if (typeof v === "object") {
      if (typeof v.toJSON === "function" && typeof v.get === "function") {
        try { return normStepDoc(v.toJSON()); } catch (_e) { return normStepDoc(null); }
      }
      return normStepDoc(v);
    }
    return normStepDoc(null);
  }

  function stepsStringFrom(v) {
    try { return JSON.stringify(stepsPlainFrom(v)); } catch (_e) { return ""; }
  }

  function isYMap(v) {
    return v && typeof v.get === "function" && typeof v.set === "function" &&
      typeof v.toJSON === "function";
  }

  function buildStepY(Y, s) {
    s = s || {};
    const sm = new Y.Map();
    STEP_SCALARS.forEach(function (k) { sm.set(k, s[k] == null ? null : s[k]); });
    sm.set("inputs", Array.isArray(s.inputs) ? s.inputs.slice() : []);
    sm.set("expecteds", Array.isArray(s.expecteds) ? s.expecteds.slice() : []);
    return sm;
  }

  function buildStepsY(Y, docPlain) {
    const d = normStepDoc(docPlain);
    const map = new Y.Map();
    map.set("input_signals", d.input_signals);
    map.set("expected_signals", d.expected_signals);
    const arr = new Y.Array();
    const cells = d.steps.map(function (s) { return buildStepY(Y, s); });
    if (cells.length) arr.push(cells);
    map.set("steps", arr);
    return map;
  }

  // Ensure rowMap[key] is a nested steps Y.Map, upgrading a legacy string/object
  // in place. MUST run inside a doc.transact().
  function ensureStepsMap(Y, rowMap, key) {
    const cur = rowMap.get(key);
    if (isYMap(cur) && typeof rowMap.get(key).get === "function") {
      // Already nested (has a "steps" child) — treat as upgraded.
      if (typeof cur.get === "function") return cur;
    }
    const map = buildStepsY(Y, stepsPlainFrom(cur));
    rowMap.set(key, map);
    return map;
  }

  function _sameJson(a, b) {
    try { return JSON.stringify(a) === JSON.stringify(b); } catch (_e) { return false; }
  }

  // Minimal diff-write of a plain StepDoc onto a nested steps Y.Map, so unchanged
  // fields don't churn and concurrent edits on OTHER fields survive the merge.
  // MUST run inside a doc.transact().
  function applyDocToStepsMap(Y, sm, docPlain) {
    const d = normStepDoc(docPlain);
    if (!_sameJson(sm.get("input_signals"), d.input_signals)) {
      sm.set("input_signals", d.input_signals);
    }
    if (!_sameJson(sm.get("expected_signals"), d.expected_signals)) {
      sm.set("expected_signals", d.expected_signals);
    }
    let arr = sm.get("steps");
    if (!arr || typeof arr.push !== "function") { arr = new Y.Array(); sm.set("steps", arr); }
    while (arr.length > d.steps.length) arr.delete(arr.length - 1, 1);
    for (let i = 0; i < d.steps.length; i++) {
      const src = d.steps[i] || {};
      let stepMap = i < arr.length ? arr.get(i) : null;
      if (!isYMap(stepMap)) {
        stepMap = buildStepY(Y, src);
        if (i < arr.length) { arr.delete(i, 1); arr.insert(i, [stepMap]); }
        else arr.push([stepMap]);
        continue;
      }
      STEP_SCALARS.forEach(function (k) {
        const nv = src[k] == null ? null : src[k];
        if (stepMap.get(k) !== nv) stepMap.set(k, nv);
      });
      const ins = Array.isArray(src.inputs) ? src.inputs : [];
      const exs = Array.isArray(src.expecteds) ? src.expecteds : [];
      if (!_sameJson(stepMap.get("inputs"), ins)) stepMap.set("inputs", ins);
      if (!_sameJson(stepMap.get("expecteds"), exs)) stepMap.set("expecteds", exs);
    }
  }

  function LMCollabController(opts) {
    this.pid = opts.pid;
    this.api = opts.api || global.LMApi;
    // Sheet catalogue + CRDT key prefix come from the server config that
    // editor.js fetched (GET /api/v1/config); the literals are only a fallback.
    this.sheets = opts.sheets || ["test", "const", "lib"];
    this.rowPrefix = opts.rowPrefix || "rows:";
    this.doc = null;
    this.provider = null;
    this.arrays = {};
    this._active = false;
    this._subs = [];
    this._changeTimers = {};
    this._cb = {};       // { onChange, onStatus, onPresence, onCursors, onRowErrors }
    this._synced = false;
    this._tok = null;        // last room token (refreshed silently before expiry)
    this._tokenTimer = null; // silent-renewal timer handle
    this._errMap = null;     // shared "row_errors" Y.Map (server-written)
    this._errHandler = null; // its observer handle
  }

  LMCollabController.prototype.isActive = function () { return this._active; };

  LMCollabController.prototype._Y = function () {
    return global.LMCollab && global.LMCollab.Y;
  };

  // Attempt to open the collaboration session. Resolves to `true` only when the
  // socket connected AND completed its initial sync; otherwise cleans up and
  // resolves to `false` so the caller transparently keeps using REST.
  LMCollabController.prototype.start = function (callbacks) {
    const self = this;
    this._cb = callbacks || {};
    return this._startAsync().catch(function (err) {
      try { console.warn("[collab] disabled:", err && err.message || err); } catch (_e) {}
      self._teardown();
      return false;
    });
  };

  LMCollabController.prototype._startAsync = async function () {
    const LMC = global.LMCollab;
    if (!LMC || !LMC.Y || !LMC.WebsocketProvider) {
      return false; // bundle not shipped → stay on REST
    }
    // Ask the server for a signed room token. A 403 (no item.edit) or 404 (old
    // server without the endpoint) simply means "no collab for this user".
    let tok;
    try {
      tok = await this.api.getCollabToken(this.pid);
    } catch (_e) {
      return false;
    }
    if (!tok || !tok.token || !tok.room) return false;

    const Y = LMC.Y;
    const doc = new Y.Doc();
    this.doc = doc;
    const self = this;
    this.sheets.forEach(function (s) {
      const arr = doc.getArray(self.rowPrefix + s);
      self.arrays[s] = arr;

      // FIX: observeDeep 对本地和远端变更都会触发。原来的代码对每次变更
      // 都调用 onChange → patchSheetData/setSheetData，导致：
      //   1. 用户每次按键后 150 ms 内 Univer 重渲染用户刚打的内容（冗余）。
      //   2. N 人同时编辑时，每人每次操作都触发其他所有人的重渲染（O(n) 渲染）。
      //
      // 修复：txn.local === true 表示事务来自本客户端，跳过 onChange。
      //   本地变更在 Univer 里已经可见（用户发起的），无需重渲染。
      //   txn.local === false 表示来自远端 peer 或服务器 materializer writeback，
      //   必须重渲染（让本地用户看到他人的改动 / 新行的真实 DB id）。
      //
      // 兼容旧版 yjs（<13.5）：txn 可能为 undefined，此时安全地走渲染路径。
      const handler = function (_evts, txn) { self._onArrayChange(s, txn); };
      arr.observeDeep(handler);
      self._subs.push({ arr: arr, handler: handler });
    });

    // Shared validation-error channel: the server materializer publishes an
    // authoritative `{ uuid: {cells, message, sheet} }` snapshot here after each
    // reconcile (design §12.2). We only observe it to paint offending cells red;
    // clients never write to it.
    const errMap = doc.getMap("row_errors");
    this._errMap = errMap;
    const errHandler = function () { self._emitRowErrors(); };
    errMap.observe(errHandler);
    this._errHandler = errHandler;

    const base = resolveWsBase(tok.ws_url);
    const provider = new LMC.WebsocketProvider(base, tok.room, doc, {
      connect: true,
      params: { token: tok.token },
    });
    this.provider = provider;

    // Presence: publish who I am so peers can show a live user list.
    try {
      const me = (global.LM && global.LM.user) || {};
      const name = me.display_name || me.username || "协作者";
      const uid = me.id || 0;
      this._selfUid = uid;
      provider.awareness.setLocalStateField("user", {
        name: name,
        id: uid,
        color: PALETTE[Math.abs(uid) % PALETTE.length],
      });
      const aw = function () { self._emitPresence(); self._emitSelections(); };
      provider.awareness.on("change", aw);
      this._awarenessHandler = aw;
    } catch (_e) { /* presence is best-effort */ }

    provider.on("status", function (e) {
      if (self._cb.onStatus) self._cb.onStatus(e.status);
    });

    // Wait for the first sync (or a timeout). Only then is the Y.Doc a faithful
    // mirror of the server state and safe to render from.
    const synced = await new Promise(function (resolve) {
      let done = false;
      const finish = function (ok) { if (!done) { done = true; resolve(ok); } };
      provider.on("sync", function (isSynced) { if (isSynced) finish(true); });
      provider.on("connection-error", function () { finish(false); });
      provider.on("connection-close", function () { finish(false); });
      setTimeout(function () { finish(self._synced); }, CONNECT_TIMEOUT_MS);
    });

    if (!synced) { this._teardown(); return false; }
    this._synced = true;
    this._active = true;
    this._tok = tok;
    this._scheduleTokenRenewal(tok.expires_in);
    this._emitPresence();
    this._emitRowErrors();
    return true;
  };

  // Read the shared error channel and hand a plain snapshot to the editor so it
  // can mark cells. Values are JSON strings the server wrote (a primitive, to
  // avoid pycrdt turning a nested dict into a Y.Map); we parse each back into
  // { cells: [field_key...], message, sheet }.
  LMCollabController.prototype._emitRowErrors = function () {
    if (!this._cb.onRowErrors || !this._errMap) return;
    const out = {};
    try {
      this._errMap.forEach(function (raw, uuid) {
        let info = raw;
        if (typeof raw === "string") {
          try { info = JSON.parse(raw); } catch (_e) { return; }
        }
        if (!info) return;
        out[uuid] = {
          cells: Array.isArray(info.cells) ? info.cells.slice() : [],
          message: info.message || "",
          sheet: info.sheet || "",
        };
      });
    } catch (_e) { return; }
    this._cb.onRowErrors(out);
  };

  // Schedule a silent token refresh before the current token expires, so any
  // future reconnect carries a valid token (see the constants above). ``expiresIn``
  // is in seconds (server-provided); falls back to 120s when absent.
  LMCollabController.prototype._scheduleTokenRenewal = function (expiresIn) {
    if (this._tokenTimer) { clearTimeout(this._tokenTimer); this._tokenTimer = null; }
    const ttlMs = (Number(expiresIn) > 0 ? Number(expiresIn) : 120) * 1000;
    const delay = Math.max(TOKEN_RENEW_MIN_MS, ttlMs - TOKEN_RENEW_SKEW_MS);
    const self = this;
    this._tokenTimer = setTimeout(function () { self._renewToken(); }, delay);
  };

  LMCollabController.prototype._renewToken = function () {
    const self = this;
    if (!this._active || !this.provider) return;
    let p;
    try { p = this.api.getCollabToken(this.pid); }
    catch (_e) { this._scheduleTokenRenewal(TOKEN_RENEW_RETRY_MS / 1000); return; }
    Promise.resolve(p).then(function (tok) {
      if (!self._active || !self.provider) return;
      if (!tok || !tok.token) { self._scheduleTokenRenewal(TOKEN_RENEW_RETRY_MS / 1000); return; }
      self._tok = tok;
      // y-websocket rebuilds the connection URL from ``provider.params`` on every
      // (re)connect, so mutating the token here makes the next reconnect use it.
      try {
        if (!self.provider.params) self.provider.params = {};
        self.provider.params.token = tok.token;
      } catch (_e) { /* provider shape differs: best-effort */ }
      self._scheduleTokenRenewal(tok.expires_in);
    }).catch(function () {
      // Session gone / network down: keep the old token, retry soon. The open
      // socket (if any) is unaffected; this only matters for a later reconnect.
      self._scheduleTokenRenewal(TOKEN_RENEW_RETRY_MS / 1000);
    });
  };

  LMCollabController.prototype.stop = function () { this._teardown(); };

  LMCollabController.prototype._teardown = function () {
    this._active = false;
    if (this._tokenTimer) { clearTimeout(this._tokenTimer); this._tokenTimer = null; }
    try {
      (this._subs || []).forEach(function (s) {
        try { s.arr.unobserveDeep(s.handler); } catch (_e) {}
      });
    } catch (_e) {}
    this._subs = [];
    try {
      if (this._errMap && this._errHandler) {
        this._errMap.unobserve(this._errHandler);
      }
    } catch (_e) {}
    this._errMap = null;
    this._errHandler = null;
    if (this.provider) {
      try {
        if (this._awarenessHandler) {
          this.provider.awareness.off("change", this._awarenessHandler);
        }
      } catch (_e) {}
      try { this.provider.destroy(); } catch (_e) {}
    }
    if (this.doc) { try { this.doc.destroy(); } catch (_e) {} }
    this.provider = null;
    this.doc = null;
    this.arrays = {};
  };

  // ---- change plumbing ------------------------------------------------- //

  LMCollabController.prototype._onArrayChange = function (sheet, txn) {
    // FIX: 跳过本地事务的重渲染。
    //
    // txn.local === true  → 本客户端写了 Y.Doc（setCell/insertRow/deleteRows
    //   等）。Univer 里已经显示了用户的变更；调度渲染只会 80 ms 后再用同
    //   样的值覆盖一次，并发时让每个用户触发其他人的渲染。
    //
    // txn.local === false → 远端 peer 或服务器 materializer 写了 Y.Doc。
    //   必须重渲染，让本地用户看到变更。
    if (txn && txn.local) return;

    const self = this;
    if (this._changeTimers[sheet]) clearTimeout(this._changeTimers[sheet]);
    this._changeTimers[sheet] = setTimeout(function () {
      self._changeTimers[sheet] = null;
      if (self._cb.onChange) self._cb.onChange(sheet);
    }, REMOTE_CHANGE_DEBOUNCE_MS);
  };

  // Emit the live online-member list (design §6.1). Awareness carries one state
  // per open connection, so a user with two tabs appears twice — dedupe by
  // ``user.id`` and keep a per-user connection count. ``self`` flags the local
  // user so the UI can label "（我）". The local user is sorted first, then the
  // rest by name, so the list order is stable as peers come and go.
  LMCollabController.prototype._emitPresence = function () {
    if (!this._cb.onPresence || !this.provider) return;
    const states = this.provider.awareness.getStates();
    const byId = {};
    const self = this;
    states.forEach(function (st) {
      const u = st && st.user;
      if (!u) return;
      const id = (u.id === null || u.id === undefined) ? ("anon:" + Math.random())
        : Number(u.id);
      if (byId[id]) { byId[id].conns += 1; return; }
      byId[id] = {
        id: id,
        name: u.name || "协作者",
        color: u.color || "#888",
        self: self._selfUid != null && Number(u.id) === Number(self._selfUid),
        conns: 1,
      };
    });
    const users = Object.keys(byId).map(function (k) { return byId[k]; });
    users.sort(function (a, b) {
      if (a.self !== b.self) return a.self ? -1 : 1;
      return String(a.name).localeCompare(String(b.name));
    });
    this._cb.onPresence(users);
  };

  // Publish the local user's SELECTION into awareness so peers can draw a border
  // overlay (design §6.1). Rows are identified by their stable ``uuid`` (never an
  // absolute row number) because peers may sort/filter and see a different order:
  //   anchor : { uuid, col } | null   -- the active cell (thick box + name tag)
  //   rows   : [ uuid... ]            -- selected rows (view-independent set)
  //   cols   : [ minCol, maxCol ] | null
  // A falsy anchor with an empty rows set clears the state.
  LMCollabController.prototype.setLocalSelection = function (sheet, anchor, rows, cols) {
    if (!this.provider) return;
    try {
      var has = (anchor && anchor.uuid) || (rows && rows.length);
      this.provider.awareness.setLocalStateField(
        "selection",
        has ? { sheet: sheet, anchor: anchor || null, rows: rows || [], cols: cols || null } : null);
    } catch (_e) { /* awareness optional */ }
  };

  // Collect remote selections and hand them to the UI as a per-sheet map
  // ``{ sheet: [ { key, name, color, anchor, rows, cols } ] }`` (local user
  // excluded). The editor maps the uuids to ids and draws each peer's overlay.
  LMCollabController.prototype._emitSelections = function () {
    if (!this._cb.onCursors || !this.provider) return;
    var states = this.provider.awareness.getStates();
    var selfId = null;
    try { selfId = this.provider.awareness.clientID; } catch (_e) { /* noop */ }
    var bySheet = {};
    states.forEach(function (st, clientId) {
      if (selfId != null && clientId === selfId) return;
      var u = st && st.user;
      var s = st && st.selection;
      if (!u || !s || !s.sheet) return;
      if (!((s.anchor && s.anchor.uuid) || (s.rows && s.rows.length))) return;
      if (!bySheet[s.sheet]) bySheet[s.sheet] = [];
      bySheet[s.sheet].push({
        key: String(u.id != null ? u.id : clientId),
        name: u.name || "协作者",
        color: u.color || "#888",
        anchor: s.anchor || null,
        rows: s.rows || [],
        cols: s.cols || null,
      });
    });
    try {
      if (window.LM_DEBUG_CURSOR) {
        console.log("[collab] _emitSelections states:", states.size,
          "self:", selfId, "bySheet:", bySheet);
      }
    } catch (_e) { /* noop */ }
    this._cb.onCursors(bySheet);
  };

  // ---- row identity helpers -------------------------------------------- //

  function rowIdOfMap(m) {
    const id = m.get("id");
    if (id !== null && id !== undefined) return Number(id);
    return tempIdFromUuid(m.get("uuid"));
  }

  LMCollabController.prototype._mapToItem = function (m, index) {
    const o = m.toJSON();
    // Re-serialise any nested steps sub-structure (item 3) back to the JSON
    // string every consumer expects. m.toJSON() turned the nested Y.Map into a
    // plain object; downstream (steps_editor, REST payloads, materialize) treats
    // this field as an opaque string, so keep that contract here.
    Object.keys(STEP_FIELDS).forEach(function (k) {
      if (o[k] != null && typeof o[k] === "object") {
        try { o[k] = JSON.stringify(o[k]); } catch (_e) { /* leave as-is */ }
      }
    });
    o.row_order = index + 1;
    if (o.id === null || o.id === undefined) o.id = tempIdFromUuid(o.uuid);
    else o.id = Number(o.id);
    if (o.version === null || o.version === undefined) o.version = 0;
    return o;
  };

  LMCollabController.prototype._indexOf = function (arr, item) {
    let idx = -1;
    const wantUuid = item && item.uuid;
    const wantId = item && item.id != null ? Number(item.id) : null;
    arr.forEach(function (m, i) {
      if (idx >= 0) return;
      if (wantUuid && m.get("uuid") === wantUuid) { idx = i; return; }
      if (wantId !== null && rowIdOfMap(m) === wantId) { idx = i; }
    });
    return idx;
  };

  // ---- read model ------------------------------------------------------ //

  LMCollabController.prototype.getItems = function (sheet) {
    const arr = this.arrays[sheet];
    if (!arr) return [];
    const out = [];
    const self = this;
    arr.forEach(function (m, i) { out.push(self._mapToItem(m, i)); });
    return out;
  };

  // ---- bulk reconcile (server-side import → shared Y.Doc) -------------- //

  LMCollabController.prototype.reconcileFromDb = function (sheet, dbItems, opts) {
    const arr = this.arrays[sheet];
    const Y = this._Y();
    if (!arr || !Y) throw new Error("协同未就绪");
    opts = opts || {};
    const removeMissing = opts.removeMissing !== false;
    const items = dbItems || [];
    this.doc.transact(function () {
      const byUuid = {};
      arr.forEach(function (m) { const u = m.get("uuid"); if (u) byUuid[u] = m; });

      const seen = {};
      items.forEach(function (it) {
        const u = it && it.uuid;
        if (!u) return;
        seen[u] = true;
        let m = byUuid[u];
        if (!m) { m = new Y.Map(); arr.insert(arr.length, [m]); byUuid[u] = m; }
        Object.keys(it).forEach(function (k) {
          if (SKIP_ON_COPY[k]) return;
          const nv = it[k];
          if (stepsCrdtEnabled() && STEP_FIELDS[k]) {
            // Excel import lands the canonical steps JSON string in the DB; fold
            // it into the nested CRDT (diff-write) so an import merges with any
            // live per-field edits instead of replacing the whole sub-structure.
            const cur = m.get(k);
            if (isYMap(cur) && _sameJson(stepsPlainFrom(cur), stepsPlainFrom(nv))) return;
            const sm = ensureStepsMap(Y, m, k);
            applyDocToStepsMap(Y, sm, stepsPlainFrom(nv));
            return;
          }
          let cur = m.get(k);
          if (typeof nv !== "object" && cur === nv) return;
          m.set(k, nv);
        });
      });

      if (removeMissing) {
        for (let i = arr.length - 1; i >= 0; i--) {
          const m = arr.get(i);
          const u = m.get("uuid");
          if (u && seen[u]) continue;
          const id = m.get("id");
          if (id !== null && id !== undefined && Number(id) > 0) arr.delete(i, 1);
        }
      }
    });
  };

  // ---- mutations (all routed through the shared Y.Doc) ----------------- //

  LMCollabController.prototype.setCell = function (sheet, item, changes) {
    const arr = this.arrays[sheet];
    if (!arr) throw new Error("协同未就绪");
    const idx = this._indexOf(arr, item);
    if (idx < 0) throw new Error("该行已被他人删除");
    const m = arr.get(idx);
    const Y = this._Y();
    this.doc.transact(function () {
      Object.keys(changes || {}).forEach(function (k) {
        if (Y && stepsCrdtEnabled() && STEP_FIELDS[k]) {
          // Route a whole-blob steps write through the nested CRDT so it merges
          // with concurrent per-field edits instead of clobbering them.
          const sm = ensureStepsMap(Y, m, k);
          applyDocToStepsMap(Y, sm, stepsPlainFrom(changes[k]));
        } else {
          m.set(k, changes[k]);
        }
      });
    });
    return this._mapToItem(m, idx);
  };

  // ---- steps sub-structure binding (item 3) ---------------------------- //

  // Return the live nested steps Y.Map for a row (creating/upgrading it), so the
  // step-detail editor can observe remote edits and write granularly. Returns
  // null when collab or the CRDT upgrade is unavailable (caller falls back to the
  // string field).
  LMCollabController.prototype.getStepsMap = function (sheet, item, key) {
    if (!stepsCrdtEnabled()) return null;
    const arr = this.arrays[sheet];
    const Y = this._Y();
    if (!arr || !Y) return null;
    const idx = this._indexOf(arr, item);
    if (idx < 0) return null;
    const m = arr.get(idx);
    let sm = null;
    this.doc.transact(function () { sm = ensureStepsMap(Y, m, key); });
    return sm;
  };

  // Commit a full StepDoc into a row's nested steps CRDT with a minimal diff so
  // concurrent per-field edits survive. Returns the materialised item (steps
  // surfaced as a string, as everywhere else).
  LMCollabController.prototype.commitSteps = function (sheet, item, key, docPlain) {
    const arr = this.arrays[sheet];
    const Y = this._Y();
    if (!arr || !Y) throw new Error("协同未就绪");
    const idx = this._indexOf(arr, item);
    if (idx < 0) throw new Error("该行已被他人删除");
    const m = arr.get(idx);
    this.doc.transact(function () {
      if (stepsCrdtEnabled() && STEP_FIELDS[key]) {
        const sm = ensureStepsMap(Y, m, key);
        applyDocToStepsMap(Y, sm, normStepDoc(docPlain));
      } else {
        m.set(key, JSON.stringify(normStepDoc(docPlain)));
      }
    });
    return this._mapToItem(m, idx);
  };

  // Read a nested steps Y.Map (or any stored representation) as a plain StepDoc.
  LMCollabController.prototype.stepsDocFrom = function (mapOrVal) {
    return stepsPlainFrom(mapOrVal);
  };

  LMCollabController.prototype.insertRow = function (sheet, opts) {
    const arr = this.arrays[sheet];
    const Y = this._Y();
    if (!arr || !Y) throw new Error("协同未就绪");
    opts = opts || {};
    const uuid = genUuid();
    const values = opts.values || {};
    let index = arr.length;
    if (opts.anchorId !== null && opts.anchorId !== undefined) {
      const anchor = Number(opts.anchorId);
      let ai = -1;
      arr.forEach(function (m, i) { if (ai < 0 && rowIdOfMap(m) === anchor) ai = i; });
      if (ai >= 0) index = opts.place === "above" ? ai : ai + 1;
    }
    const m = new Y.Map();
    this.doc.transact(function () {
      arr.insert(index, [m]);
      m.set("uuid", uuid);
      Object.keys(values).forEach(function (k) {
        if (!SKIP_ON_COPY[k]) m.set(k, values[k]);
      });
    });
    return this._mapToItem(m, index);
  };

  LMCollabController.prototype.deleteRows = function (sheet, ids) {
    const arr = this.arrays[sheet];
    if (!arr) throw new Error("协同未就绪");
    const want = new Set((ids || []).map(Number));
    const idxs = [];
    arr.forEach(function (m, i) { if (want.has(rowIdOfMap(m))) idxs.push(i); });
    if (!idxs.length) return 0;
    this.doc.transact(function () {
      for (let j = idxs.length - 1; j >= 0; j--) arr.delete(idxs[j], 1);
    });
    return idxs.length;
  };

  LMCollabController.prototype.duplicateRows = function (sheet, ids) {
    const arr = this.arrays[sheet];
    const Y = this._Y();
    if (!arr || !Y) throw new Error("协同未就绪");
    const want = new Set((ids || []).map(Number));
    const targets = [];
    arr.forEach(function (m, i) {
      if (want.has(rowIdOfMap(m))) targets.push({ i: i, json: m.toJSON() });
    });
    if (!targets.length) return 0;
    targets.sort(function (a, b) { return b.i - a.i; });
    this.doc.transact(function () {
      targets.forEach(function (t) {
        const clone = new Y.Map();
        arr.insert(t.i + 1, [clone]);
        Object.keys(t.json).forEach(function (k) {
          if (SKIP_ON_CLONE[k] || k === "case_id") return;
          clone.set(k, t.json[k]);
        });
        clone.set("uuid", genUuid());
      });
    });
    return targets.length;
  };

  LMCollabController.prototype.moveRows = function (sheet, ids, dir) {
    const arr = this.arrays[sheet];
    const Y = this._Y();
    if (!arr || !Y) throw new Error("协同未就绪");
    const want = new Set((ids || []).map(Number));
    const idxs = [];
    arr.forEach(function (m, i) { if (want.has(rowIdOfMap(m))) idxs.push(i); });
    if (!idxs.length) return;
    const minI = Math.min.apply(null, idxs);
    const maxI = Math.max.apply(null, idxs);
    const up = !(dir === "down" || dir === "DOWN" || dir === 1);
    if (up && minI <= 0) return;
    if (!up && maxI >= arr.length - 1) return;
    this.doc.transact(function () {
      const from = up ? minI - 1 : maxI + 1;
      const json = arr.get(from).toJSON();
      arr.delete(from, 1);
      const to = up ? maxI : minI;
      const clone = new Y.Map();
      arr.insert(to, [clone]);
      Object.keys(json).forEach(function (k) {
        if (!SKIP_ON_COPY[k]) clone.set(k, json[k]);
      });
    });
  };

  global.LMCollabController = {
    create: function (opts) { return new LMCollabController(opts); },
    _tempIdFromUuid: tempIdFromUuid, // exported for tests
  };
})(window);
