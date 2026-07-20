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
  // FIX: debounce only remote change renders. Local edits are already in
  // Univer (the user typed them), so we only need the remote path, and 80 ms
  // is snappy enough without flooding the render queue.
  const REMOTE_CHANGE_DEBOUNCE_MS = 80;

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

  function LMCollabController(opts) {
    this.pid = opts.pid;
    this.api = opts.api || global.LMApi;
    this.sheets = opts.sheets || ["test", "const", "lib"];
    this.doc = null;
    this.provider = null;
    this.arrays = {};
    this._active = false;
    this._subs = [];
    this._changeTimers = {};
    this._cb = {};       // { onChange, onStatus, onPresence }
    this._synced = false;
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
      const arr = doc.getArray("rows:" + s);
      self.arrays[s] = arr;
      // FIX: The observeDeep handler receives (events, transaction).
      // In Yjs, transaction.local === true for writes originating from THIS
      // client, false for writes from a remote peer or the server materializer.
      //
      // Previously every change — local and remote — fired onChange, which
      // caused editor.js to call patchSheetData/setSheetData on every local
      // write (the user edits a cell → Y.Map.set → observer fires → Univer
      // re-renders what the user just typed). This caused:
      //   1. A redundant 150 ms render on every local keystroke.
      //   2. Under concurrent edits, every participant triggered a re-render
      //      for every other participant's keystrokes — O(n) renders per edit.
      //
      // Fix: skip the onChange for local transactions. Local changes are
      // already visible in Univer (the user typed them or triggered the
      // mutation); only remote changes need a render pass.
      //
      // Exception: materializer writeback (id/version flip on new rows) is a
      // remote transaction (origin = provider) — it correctly falls through to
      // onChange so the grid replaces the temp negative id with the real DB id.
      const handler = function (_evts, txn) { self._onArrayChange(s, txn); };
      arr.observeDeep(handler);
      self._subs.push({ arr: arr, handler: handler });
    });

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
      provider.awareness.setLocalStateField("user", {
        name: name,
        id: uid,
        color: PALETTE[Math.abs(uid) % PALETTE.length],
      });
      const aw = function () { self._emitPresence(); };
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
    this._emitPresence();
    return true;
  };

  LMCollabController.prototype.stop = function () { this._teardown(); };

  LMCollabController.prototype._teardown = function () {
    this._active = false;
    try {
      (this._subs || []).forEach(function (s) {
        try { s.arr.unobserveDeep(s.handler); } catch (_e) {}
      });
    } catch (_e) {}
    this._subs = [];
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
    // FIX: Skip re-render for transactions that originated locally.
    //
    // txn.local === true  → this client wrote the Y.Doc (setCell, insertRow,
    //   deleteRows, etc.). Univer already shows the user's change; scheduling
    //   a render would just overwrite the same values 80 ms later and, under
    //   concurrent use, makes each user trigger a render on every peer.
    //
    // txn.local === false → a remote peer or the server materializer wrote the
    //   Y.Doc. We MUST re-render so the local user sees the change.
    //
    // Note: txn may be undefined in older yjs versions (< 13.5). The guard
    // `txn && txn.local` safely falls through to the render path in that case.
    if (txn && txn.local) return;

    const self = this;
    if (this._changeTimers[sheet]) clearTimeout(this._changeTimers[sheet]);
    this._changeTimers[sheet] = setTimeout(function () {
      self._changeTimers[sheet] = null;
      if (self._cb.onChange) self._cb.onChange(sheet);
    }, REMOTE_CHANGE_DEBOUNCE_MS);
  };

  LMCollabController.prototype._emitPresence = function () {
    if (!this._cb.onPresence || !this.provider) return;
    const states = this.provider.awareness.getStates();
    const users = [];
    states.forEach(function (st) { if (st && st.user) users.push(st.user); });
    this._cb.onPresence(users);
  };

  // ---- row identity helpers -------------------------------------------- //

  function rowIdOfMap(m) {
    const id = m.get("id");
    if (id !== null && id !== undefined) return Number(id);
    return tempIdFromUuid(m.get("uuid"));
  }

  LMCollabController.prototype._mapToItem = function (m, index) {
    const o = m.toJSON();
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

  // Bring a sheet's Y.Array in line with a set of authoritative DB rows. Used
  // after a REST/Excel import: those rows land in the DB directly, so while
  // collaboration is live they are invisible to peers (the grid reads the Y.Doc)
  // AND the one-way materializer would soon soft-delete them. Injecting them into
  // the Y.Doc — keyed by their real `uuid` so the materializer matches instead of
  // duplicating — makes them show up for everyone and survive materialization.
  //
  // dbItems: plain row dicts straight from the DB (each MUST carry `uuid`; `id`
  // and `version` are kept so the grid keys correctly and no temp id is used).
  // opts.removeMissing (default true): drop rows that were previously
  // materialized (real positive `id`) but are absent from `dbItems` — e.g. a
  // "replace all" import. Never-materialized local rows (temp/negative id) are
  // left untouched so a peer's in-flight insert is not clobbered.
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
        if (!u) return;                       // a DB row with no uuid: skip
        seen[u] = true;
        let m = byUuid[u];
        if (!m) { m = new Y.Map(); arr.insert(arr.length, [m]); byUuid[u] = m; }
        Object.keys(it).forEach(function (k) {
          if (SKIP_ON_COPY[k]) return;        // row_order/updated_at are implicit
          const nv = it[k];
          let cur = m.get(k);
          // Cheap equality for primitives; always rewrite objects/arrays (rare).
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
    this.doc.transact(function () {
      Object.keys(changes || {}).forEach(function (k) { m.set(k, changes[k]); });
    });
    return this._mapToItem(m, idx);
  };

  // opts: { anchorId, place: "above"|"below", values }
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
    // Insert each clone right after its source; process bottom-up so earlier
    // indices stay valid.
    targets.sort(function (a, b) { return b.i - a.i; });
    this.doc.transact(function () {
      targets.forEach(function (t) {
        const clone = new Y.Map();
        arr.insert(t.i + 1, [clone]);
        Object.keys(t.json).forEach(function (k) {
          // Drop identity + case_id so the server assigns a fresh uuid/id and
          // auto-generates a unique case_id (mirrors REST duplicate).
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
    // Neighbour-hop: move the single adjacent row across the block. Only one row
    // is delete+re-inserted, so the moved block keeps its CRDT identity.
    if (up && minI <= 0) return;
    if (!up && maxI >= arr.length - 1) return;
    this.doc.transact(function () {
      const from = up ? minI - 1 : maxI + 1;
      const json = arr.get(from).toJSON();
      arr.delete(from, 1);
      // after removing `from`, the block shifted iff from < block; recompute the
      // neighbour's new resting index.
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
