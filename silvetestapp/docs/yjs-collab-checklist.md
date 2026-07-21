# silvetestapp 协同实施 Checklist（Yjs · Matrix 优先）

> 决策已定：**服务端 = Python（pycrdt + pycrdt-websocket + uvicorn，独立进程）**；**Doc 粒度 = 每 project 一个 Doc（含 test/const/lib 三个 sheet，各为一个 `Y.Array<Y.Map>`）**。
> 配套设计见 `yjs-collab-design.md`。本清单为“可勾选”的落地步骤，按阶段推进，每阶段结束都保持**可回退、可降级**。
> 约束提醒：`frontend/` 的 TS 改动（含 Univer 多 sheet API 恢复）必须在**有网机器**上 `npm install && npm run build`，沙箱无 npm。

---

## Phase 0 — 打底（无行为变化，可先合入）

**目标**：把依赖、进程骨架、鉴权、存储、服务层事务开关都准备好，但不改变任何现有编辑行为。

> **前置阻塞（先做，否则 adapter 协同改造无从谈起）**
> - [ ] 在**有网机器**上 `cd frontend && npm install && npm run build`，恢复 Univer 多 sheet API（`univer.full.umd.js` 含 `setSheetFields`），使主表脱离 FallbackGrid。见设计文档 §12.3 第 4 点。

### 0.0 服务层事务改造（唯一需动 `items_service`，向后兼容） — ✅ 已实现（待你在装了 Flask/PG 的环境跑测试）
- [x] 给写函数（`update_item`/`create_item`/`soft_delete_item`/`restore_item`/`move_items`/`bulk_soft_delete`/`bulk_duplicate`）增加可选参数 `commit: bool = True`；`False` 时**只 flush 不 commit**，由调用方统一提交。默认 `True`，现有 REST 路径行为不变。
- [x] 新增**物化专用变体**（`items_service` 尾部新增区块）：
  - `find_row_by_uuid(project_id, uuid, include_deleted=False)` — 按 CRDT 行身份查行。
  - `materialize_create(user, project, state, sheet=, row_order=, commit=False)` — 保留 Y.Map 的 `uuid`、自动补 `case_id`、跳过 spec 校验。
  - `materialize_update(user, project, item, changes, commit=False)` — **不抛 `VersionConflict`**（自愈），软删行会被复活，`version` 仍自增供 SSE/导出感知。
  - `materialize_sheet(user, project, sheet, rows, commit=True)` — 整表对账：uuid upsert + `row_order`=快照下标 + 缺失 uuid 软删，整趟一个事务。`rows` 即 `Y.Array.to_py()`。
- [x] 单测 `tests/test_lanmatrix_materialize.py`：create/update/reorder/delete/resurrect、无 uuid 跳过、`VersionConflict` 不抛、`commit=False` 延迟持久化（6 个用例，DB 版，依赖 `conftest.py` 的 PG 测试库）。
- [ ] **▶ 你来跑**：`pytest tests/test_lanmatrix_materialize.py`（本沙箱无 Flask/PG，只做了 `py_compile`）。
- 注：三份工作树（`f7` / `silvetestapp` / `silvetestapp-fix8`）已同步同一改动（原本字节相同）。

### 0.1 依赖与构建
- [ ] 前端：`frontend/package.json` 增加 `yjs`、`y-protocols`、`y-websocket`（客户端）。
- [ ] 确认离线安装可行：在有网/内网 registry 预取上述包（沙箱只有 py wheel，务必提前备好 tarball）。
- [ ] vite 新增入口 `frontend/src/collab.ts` → 产出 `collab.umd.js`，挂 `window.LMCollab`（`{ Doc, WebsocketProvider, encodeStateAsUpdate, applyUpdate, ... }`）。
- [ ] 后端：`requirements.txt` 增加 `pycrdt`、`pycrdt-websocket`、`uvicorn`（生产可加 `websockets`/`anyio` 传递依赖）。
- [ ] `pip install -r requirements.txt` 验证可装（注意 `pycrdt` 是 Rust 轮子，确认目标平台有 wheel 或可编译）。

### 0.2 存储（PostgreSQL，同库）
- [ ] 新增表 `lm_collab_doc`：`project_id`(FK, unique) / `state`(bytea, Yjs update) / `updated_at`。存 Doc 快照。
- [ ] 新增表 `lm_collab_update`（可选，用于增量/审计）：`id` / `project_id` / `update`(bytea) / `created_at`。用于 append-only 增量，定期 compaction 进 `lm_collab_doc`。
- [ ] 生成迁移脚本（沿用项目现有迁移机制），本地 upgrade/downgrade 验证。

### 0.3 鉴权
- [x] Flask 增加 `POST /api/v1/projects/<id>/collab-token`：`login_required` + `item.edit` 校验 → 返回 itsdangerous 签名短期 token（含 `uid/un/pid/role/room`，salt 命名空间，默认 120s 过期）。实现见 `app/collab/tokens.py` + `lanmatrix_api.py::collab_token`。
- [x] 共享密钥：复用 `Config.SECRET_KEY`（web 签发、协同进程校验），无需额外密钥管理。
- [ ] 前端拿 token 的封装（`POST /api/v1/projects/<id>/collab-token`，带 `X-CSRF-Token`），含过期前续签逻辑占位。（Phase 1.3 前端一并做）

### 0.4 协同进程骨架（先能连、不物化） — ✅ 代码已就绪（本沙箱无 pycrdt，只 `py_compile`；待你在装库环境跑）
- [x] 新增 `run_collab.py` + `app/collab/server.py`：uvicorn 启 `ASGIServer(ws_server, on_connect=鉴权)`。
- [x] Room 命名 = `project:{id}`；`on_connect(scope)` 校验 token 且 `payload.room == scope.path`。
- [x] Doc 持久化后端 `app/collab/pg_ystore.py`（`BaseYStore` 子类，读/写 `lm_collab_doc`，`merge_updates` 压缩）；新增模型 `CollabDoc` + `db.create_all()` 自动建表。
- [x] 首次进房水合：`PgYStore.read()` 回放；空则 `doc_model.bootstrap_doc` 从 DB 播种并写回 store。
- [ ] **▶ 你来跑冒烟**：`pip install pycrdt pycrdt-websocket uvicorn`（见 `requirements-collab.txt`）→ `python run_collab.py` → 两个浏览器/`wscat` 连 `ws://host:1234/project:1?token=…` 同步一个 `Y.Map`。
- [x] 部署文档：三进程并列（`run_web.py` / `run_worker.py` / `run_collab.py`）；反代透传 `Upgrade`/`Connection` 头到 1234 端口。已写入 `README.md`「Run → Real-time collaboration」：三进程启动、单 worker 约束、nginx `Upgrade`/`Connection` 透传示例、`COLLAB_WS_URL`/`COLLAB_REST_GUARD` 等环境变量表、单一写者边界与优雅降级说明；`run_collab.py` 亦补建 `CollabPresence` 表。

> ⚠️ **VERIFY-ONCE（已在 `server.py`/`pg_ystore.py` 顶部标注）**：① `ASGIServer(on_connect)` 回调元数（用 `*args` 容错）；② `WebsocketServer.get_room(name)` 是每连接派发入口、`start_room(room)` 挂预建房间；③ room 名 == `scope['path']` 去掉前导 `/`。均为已确认的公有类/方法上的**调用契约**假设，装库后一次性核对即可。

**Phase 0 验收**：现有站点行为零变化；协同进程可独立起停；两客户端能在裸 Y.Doc 上同步。

---

## Phase 1 — Matrix 行级协同（MVP，核心）

**目标**：Matrix 总表进入 CRDT 驱动，多人实时合并 + 服务端物化回 `TestItemRow`。

### 1.1 CRDT 数据模型（前后端共识，先写成规范）
- [ ] Doc 顶层：`ymap("sheets")` → key ∈ {`test`,`const`,`lib`} → 每个 value 是 `Y.Array<Y.Map>`（一行一个 `Y.Map`）。
- [ ] 行 `Y.Map` 字段：`uuid`(必填, client 生成 32 位) / `id`(number|null, 服务端回写) / 各业务字段（`case_id/title/module/precondition/test_steps/expected_result/actual_result/result/priority/owner/tags/comment/workflow_status`) / `custom_values`(嵌套 `Y.Map` 或 JSON) / `steps`(JSON 字符串, Phase 3 再拆)。
- [ ] **顺序即 `Y.Array` 下标**：不再用 `row_order` 决定 CRDT 顺序（物化时把下标写入 DB `row_order`）。
- [ ] 版本：`version` 不进 CRDT（乐观锁是 DB 概念）；服务端物化后把新 `id/version` 回写 `Y.Map` 供各端知晓。

### 1.2 服务端物化对账（协同进程内，去抖触发） — ✅ 代码已就绪（`app/collab/materializer.py`）
- [x] Doc 变更去抖（默认 500ms，`Doc.observe(TransactionEvent)` 钩子）后，对每个 sheet 的 `Y.Array` 做一次对账（`materialize_sheet`）：
  - [x] 以 `uuid` 为键 upsert `TestItemRow`（`project_id`+`sheet`+`uuid`）。
  - [x] `row_order` = 该行在 `Y.Array` 中的下标。
  - [x] CRDT 中不存在、DB 中存在的行 → 软删除（`deleted_at`）；重现的 uuid 复活。
  - [x] 新行落库后，把生成的 `id`/`version` 写回对应 `Y.Map`。已实现：`_materialize_sync` 提交后用 `items_service.sheet_uuid_index` 取 `uuid→(id,version)`，回到事件循环线程经 `Materializer._apply_id_writeback` → `doc_model.write_back_ids` 写回，包在 `suppressed()` + `origin='materialize-writeback'` 事务里，避免回环。
  - [x] `custom_values`/`tags` 走 `set_field` 清洗（NOT NULL 不写 NULL）。
- [x] 一次对账 = **一个事务**：per-sheet `materialize_sheet(commit=False)` + 末尾统一 `db.session.commit()`。
- [x] 更新用 §0.0 的 `materialize_update`（自愈、不抛 `VersionConflict`）；协同期以 CRDT 为权威。
- [ ] 物化用**独立 scoped session / 限连接数**：当前用 `anyio.to_thread` + Flask app_context 复用 `db.session`（每线程独立）；上线前评估连接池上限（见设计文档 §12.3 第 3 点）。
- [x] 单飞 flush + 脏标记重跑（flush 期间新变更会在结束后补跑一次），异常只记日志不崩事件循环。
- [ ] 物化用**独立 scoped session**（不与 web 请求共享）；**限制连接数**避免打爆 PG（现 waitress threads≥16 + worker 16 同库）。失败不影响 CRDT，仅记日志 + 重试。见设计文档 §12.3 第 3 点。
- [x] **审计**：`updated_by` 取该批次 awareness 参与者中正在编辑该行者（拿不到则回退批次 actor / 系统用户）。已实现：新增纯函数模块 `app/collab/awareness.py`（`snapshot_states` 防御式读取 room Awareness、`row_actors` 由各端 `cursor/selection.uuid` → `user.id` 建 `uuid→uid` 映射，同行按 client_id 稳定取最高者）。`server.py` 进房时 `mat.attach(ydoc, awareness=room.awareness)`；`Materializer._flush` 快照 awareness → `_resolve_row_actors` 查活跃 `LMUser` → 作为 `actor_by_uuid` 传入 `items_service.materialize_sheet`（新增可选参数，逐行归属 create/update；软删仍用批次 actor）。完全向后兼容：无 awareness/无 pycrdt 时退化为单一 actor，行为不变。纯逻辑单测 `tests/test_collab_awareness.py`（15 例，DB-free）。
  - [ ] **▶ 你来跑**：装 pycrdt/PG 环境后端到端核对 awareness 字段形状（`snapshot_states` 已对 `states` 属性 / `get_states()` 两种 API 容错，异常即回退）。

### 1.3 前端 adapter.ts 改造（CRDT 驱动）
- [ ] 数据源：`ctx.items` 缓存改为“从 `Y.Array` 投影”；初始化时 `provider.on('sync')` 后首绘。
- [ ] 本地编辑：`_flushSync` 的“逐行 PATCH”替换为**写 Y.Map**（放进一个 `doc.transact`，`origin='local-edit'`）。
- [ ] 远端变更：`yarray.observeDeep` → 增量把变化行/单元格 `setSheetData` 回 Univer（`origin!=='local-edit'` 才回绘，避免回环）。
- [ ] 行操作映射到 `Y.Array` 事务：
  - [ ] `onInsert` → `yarray.insert(idx, [newYMap(uuid)])`
  - [ ] `onDelete`/`onBulkDelete` → `yarray.delete(idx, n)`
  - [ ] `onMove` → `delete`+`insert`（同一 transact）
  - [ ] `onBulkDuplicate` → 复制 `Y.Map` 内容、生成新 uuid、`insert`
- [ ] 保留 `id`/`version` 只读展示；不再本地维护乐观锁（改由物化回写驱动）。

### 1.4 视图态与 CRDT 解耦（关键正确性）
- [ ] 筛选 / 排序 / 查找**只作用于 Univer 显示层**，绝不调用 `Y.Array` 的移动/删除。
- [ ] 明确“显式拖拽/移动行”才写 CRDT 顺序；排序按钮不写。
- [ ] 加一层“视图行 index ↔ CRDT 行 uuid”的映射，保证在筛选/排序视图下的单元格编辑仍能定位到正确 `Y.Map`。

### 1.5 优雅降级
- [ ] 协同进程不可达 / token 失败 → 前端回退到**现状逐行 PATCH + version 冲突刷新**路径（保留旧代码路径，用 feature flag / 连接失败自动切换）。
- [ ] Univer 旧 bundle（缺多 sheet API）时仍走 FallbackGrid（既有逻辑，不动）。

### 1.6 单一写者边界（最需纪律，见设计文档 §12.3 第 1/2 点） — ✅ 代码已就绪（默认关闭，opt-in；本沙箱只 `py_compile`）
- [x] 定义“协同态”判定与跨进程信号：新增 `lm_collab_presence` 表（`project_id` PK / `connections` / `updated_at`，`CollabPresence` 模型，`db.create_all` 自动建表）。协同进程 `run_collab` 按心跳（`COLLAB_PRESENCE_HEARTBEAT_SECONDS`，默认 10s）写入活跃房间连接数，房间被驱逐 / 进程退出时置 0；web 进程读该表判定“协同态”= `connections>0` 且 `updated_at` 新鲜（`COLLAB_PRESENCE_TTL_SECONDS`，默认 30s）。实现见 `app/collab/presence.py` + `server.py::_heartbeat_loop/_clear_presence_sync`。
- [x] project 进入协同态时，DB 的**唯一写者是物化路径**：web 侧 REST 行写入在协同态下被拒绝/引导。新增路由守卫 `_collab_write_blocked`（`projects_items.py`），套在 `create/patch/delete/duplicate/restore/bulk-delete/bulk-duplicate/move/batch-update/batch-undo` 上，命中时返 `409 COLLAB_ACTIVE` + 中文引导语。
- [x] 确保协同态下**无任何非物化路径调用 `move_items`**：`move_items` 路由已挂守卫，协同态直接 409（它会全表规整 `row_order`，与 CRDT 顺序打架）。物化路径不经路由、直接调 `items_service`，不受守卫影响。
- [x] **优雅降级自洽**：协同进程崩溃 → 心跳停 → presence 转陈旧 → 守卫自动放行 → 恢复常规乐观锁 REST（与 §1.5 一致）。守卫 fail-open：presence 查询异常也放行，绝不因记账故障阻断编辑。
- [x] **feature-flag 可回退**：`COLLAB_REST_GUARD`（默认 `False`）关闭整个守卫，行为与改造前完全一致；导入路径（`import_*`）**不挂守卫**，沿用“写库→前端 `resyncSheetFromDb` 折回 Y.Doc”的既有 Phase-1 流程。
- [x] 纯逻辑单测 `tests/test_collab_presence.py`（6 例，DB-free，测 `is_fresh` 新鲜度判定；沙箱无 flask 无法 import，逻辑已离线等价验证通过）。
- [ ] **▶ 你来跑**：装 flask 环境 `pytest tests/test_collab_presence.py`；装 pycrdt/PG 后端到端验证——开协同→另一 REST 客户端 `COLLAB_REST_GUARD=1` 时被 409、杀协同进程 30s 后恢复放行。

**Phase 1 验收**：
- [ ] 两人同时改不同单元格 → 实时互显、无覆盖。
- [ ] 一人插入/删除/拖拽行 → 另一人顺序一致。
- [ ] 新行数秒内被物化，两端都看到分配的 DB `id`。
- [ ] 一人本地筛选/排序，**不影响**他人顺序。
- [ ] 协同进程手动杀掉 → 前端自动降级到 PATCH，仍可编辑保存。

---

## Phase 2 — 体验增强

- [ ] awareness：正在编辑的行高亮、远端光标/选区、在线成员列表。
- [ ] Excel 导入走 `Y.Array` 批量事务（一次大 transact，避免逐行抖动）。
- [ ] 断线重连、token 到期前静默续签、离线编辑缓冲后重放。
- [ ] 大矩阵（数千行）性能压测：首次 sync 时延、内存、Univer 虚拟化在 CRDT 投影下仍生效。
- [ ] 单元格级校验错误回报（某行非法不阻断他行物化）。

---

## Phase 3 —（顺带）Steps 明细协同

- [ ] 复用同一 collab 基座；把行内 `steps` JSON 升级为子结构 CRDT（`Y.Array` 步骤 + `Y.Map` 字段）。
- [ ] `steps_editor.js` 打开明细时绑定对应行 `Y.Map` 下的 steps 子结构（替换当前 destroy/remount 单例方案）。
- [ ] 优先级低于 Matrix，Matrix 稳定后再做。

---

## 交付与回退纪律

- [ ] 每阶段独立可合入、可 feature-flag 关闭。
- [ ] 打包沿用现有模式：清 `__pycache__` → `shutil.make_archive`；核对 zip 无 `.pyc`/`_t_` 临时文件。
- [ ] 每次含 `frontend/` TS 改动的交付，附带提醒：**需在有网机器 `npm install && npm run build`** 才生效。

---

## 立即可并行启动的三件事（不阻塞彼此）

1. **备离线 npm 包 + 装 `pycrdt`**：验证目标平台能装上 CRDT 库（最大不确定性，先探）。
2. **Phase 0.2/0.3**：`lm_collab_doc` 表 + 迁移 + `/collab/token` 路由（纯后端，独立于前端构建）。
3. **CRDT 数据模型规范（1.1）定稿**：前后端就 `Y.Map` 字段名/类型达成书面共识，后续两侧并行开发。
