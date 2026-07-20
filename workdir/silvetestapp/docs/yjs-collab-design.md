# silvetestapp 协同架构设计（Yjs · Matrix 优先）

> 状态：设计草案（讨论用，**未改动任何源码**）
> 目标：在**不购买 Univer Sheet Pro 协作服务**的前提下，为 **Test Matrix 总表**引入基于 **Yjs (CRDT)** 的多人实时协同，同时保留 Excel/Spreadsheet 操作体验。
> 依据用户定调：**协同热点是 Matrix 总表，而非单个 TestCase 的 Steps 编辑器**；Matrix 是业务核心，也是主要工作界面，必须优先保证 Spreadsheet 体验。

---

## 0. TL;DR（结论先行）

- **协同对象 = Matrix 总表的“行集合”**：把每个逻辑 sheet（test/const/lib）建模成 **`Y.Array<Y.Map>`**（一行一个 `Y.Map`，字段级合并）。行的**插入/删除/移动/排序**由 CRDT 的数组语义天然无冲突合并——正好覆盖用户列出的高频场景（批量增改、改负责人/优先级/状态、拖拽调序、批量编辑）。
- **保留 Excel 体验**：继续用 Univer 社区版做交互层，**不碰 Univer Pro 的闭源协作插件**。把现有 `adapter.ts` 从「按行 PATCH」改造成「**CRDT 驱动**」：本地编辑 → 写 `Y.Map`；远端变更 → 投影回 Univer 单元格。复制粘贴/查找替换/筛选/排序/导入导出等**仍是 Univer 自带能力**（筛选/排序为视图态，不改 CRDT 顺序）。
- **数据仍是权威结构化模型**：CRDT 是「交互与合并层」，服务端**去抖物化**成 `TestItemRow` 行（新增/更新/删除/重排 `row_order`）。runner/export/Excel 导出全链路**零改动**。
- **传输**：waitress 是同步 WSGI**不能跑 WebSocket** → 新增**独立协同进程**（推荐 Python `pycrdt-websocket`+uvicorn，同栈复用鉴权与物化逻辑）。Flask 主进程不变。
- **新行标识**：复用 `TestItemRow.uuid`。行在 CRDT 里用 client 生成的 uuid 作 key；服务端物化时分配 DB `id` 并写回 `Y.Map`，各端据此得知 id。解决「未落库新行」的引用问题。

```text
   TestCase 数据模型 (Source of Truth, PostgreSQL)
            ↕  物化 / 加载 (去抖 upsert + row_order)
      Matrix CRDT (Y.Array<Y.Map>  per sheet)   ← 协同合并层
            ↕  Matrix Adapter (adapter.ts, CRDT 驱动)
        Univer Sheet (Spreadsheet UX：复制/粘贴/查找/筛选/排序/拖拽/导入导出)
```

---

## 1. 现状盘点（本次代码勘察结论）

| 维度 | 现状 |
|---|---|
| Web 框架 / 服务器 | Flask + Flask-SQLAlchemy；**waitress 同步多线程 WSGI**（`run.py`），无 WebSocket/ASGI |
| 进程模型 | web (`run_web.py`) + 独立 Huey worker (`run_worker.py`) |
| 数据库 | **PostgreSQL only**（Huey 队列同库） |
| 前端表格 | Univer Sheets **社区版**（`frontend/src/adapter.ts` → vite → `vendor/univer/univer.full.umd.js`，`window.LMUniver`）；无 Pro 协作 |
| Matrix 模型 | 每个逻辑 sheet(test/const/lib) = 一个 Univer worksheet；**行 = `TestItemRow`**，列 = 字段（系统列 + `custom_values` JSON 动态列） |
| 行标识 | `TestItemRow.id`(自增) + **`uuid`(32位, 已有列)** + `row_order`(int, 排序) + `version`(乐观锁) |
| 单元格保存 | `adapter._flushSync`：事件去抖(90ms) → 读区域 diff 本地缓存 → **按 item 分组** → `onSave({id,version}, changes)` → **逐行 PATCH** `items/{id}` |
| 冲突处理 | 服务端 `update_item` 比对 `version`，不一致抛 `VersionConflict`；前端捕获 `VERSION_CONFLICT` → 用 `server_data/server_version` **刷新该行**（被动、事后） |
| 行生命周期 | `onInsert/onDelete/onMove/onBulkDelete/onBulkDuplicate` → `items_service`（`move_items`/`bulk_duplicate`/`bulk_soft_delete` 会重排 `row_order`） |
| 实时性 / presence | **无**。他人改动不推送；无“谁在编辑哪一行”的存在感 |
| Excel 导入导出 | `excel_service`/`matrix_excel`；导出读 DB 行，导入写 DB 行 |

**协同痛点**：多人同时增行/改状态/调序时，只有事后 `version` 冲突刷新，体验割裂；无实时可见性；批量操作互相顶掉。

---

## 2. 关键决策：行级 CRDT（不是表格级、也不是文档级）

- **不做表格级 CRDT**（把整个 Univer 电子表格接 Yjs）：Univer 协作是 Pro 闭源插件，社区版无绑定点，逆向 mutation/公式/样式成本极高且随版本易碎。
- **不做（本期）Steps 文档级 CRDT 为主**：用户明确协同热点在 Matrix，Steps 只是次要（见 §9 Phase 3 顺带）。
- **做行级 CRDT**：`sheet → Y.Array<Y.Map(row)>`。理由：
  - 与现有「行=item、按行保存、按行冲突」模型**同构**，改造面可控。
  - CRDT 数组**天然解决插入/删除/移动的并发合并**（用户第一诉求）。
  - `Y.Map` 字段级合并 → 两人改同一行不同字段不冲突；改同一字段由 CRDT LWW 决定，仍比“整行顶掉”好。
  - 视图态操作（筛选/排序/查找）**不进 CRDT**，保持 Univer 本地体验，零协同副作用。

> 一句话：**把“行”当协同单元，用 Yjs 的 Array+Map 取代“逐行 PATCH + 事后 version 冲突”。**

---

## 3. CRDT 数据模型

### 3.1 结构
```
projectDoc (Y.Doc, 每个 project 一份；或每 sheet 一份，见 §3.4)
 ├─ Y.Array  rows_test      // 每元素 = Y.Map(row)
 ├─ Y.Array  rows_const
 └─ Y.Array  rows_lib

row = Y.Map {
   uuid:      string        // 客户端生成，行的稳定身份（= TestItemRow.uuid）
   id:        number|null   // DB 主键；新行为 null，物化后由服务端回填
   version:   number        // 保留：物化时的乐观锁校验点
   fields:    Y.Map {       // 字段值（系统列 + 动态列统一放这里）
       case_id, title, module, owner_id, priority, workflow_status,
       result, comment, tags, ...customKey: value,
       steps: string        // 仍是 JSON 字符串（Steps 明细，Phase 3 再细化）
   }
}
```
- **行顺序 = `Y.Array` 的下标顺序**，即业务 `row_order`。插入/拖拽/上下移 = `Y.Array` 的 insert/move（CRDT 合并无冲突）。
- **`steps` 字段本期仍存 JSON 字符串**（Matrix 单元格只显示“步骤明细(N)”，编辑走抽屉）——Matrix 协同不需要拆开它。

### 3.2 身份与新行（重点）
- 新行在前端先生成 `uuid`，`id=null` 插入 `Y.Array`。
- 服务端物化时：`uuid` 命中已有行→update；未命中→**INSERT** 并把新 `id/version` 写回该 `Y.Map`（各端观察到 id）。
- 删除：从 `Y.Array` 移除该 `Y.Map` → 服务端据“消失的 uuid”做 `soft_delete`。
- 好处：不依赖“先落库拿 id 再引用”，离线也能建行，合并后再对账。

### 3.3 与现有乐观锁的关系
- 协同开启期间，**协同服务是该 sheet 行的唯一写者**：它读当前 DB `version`→物化→`version+1`。
- 若非协同路径（如 Excel 导入、后台脚本）改了库导致 `version` 漂移：物化时重读最新 `version` 重试；因为 **CRDT 内容视为该字段/行的权威**，可安全覆盖并把最新 `version` 回写 `Y.Map`。
- 关房后（无活跃协同）恢复常规 `update_item` 乐观锁语义。

### 3.4 一个 Doc 的粒度：project vs sheet
- **推荐：每 project 一个 `Y.Doc`，内含 3 个 `Y.Array`**（test/const/lib），房间键 = `project:<id>`。切 tab 只切 `Y.Array`，同库同房便于 presence。
- 若单 project 行数极大（数万行）→ 退化为**每 sheet 一个 Doc**（`project:<id>:<sheet>`）按需加载，降低单 Doc 内存/带宽。
- 决策放 §11 待定（取决于最大矩阵规模）。

---

## 4. 目标架构（分层 + 进程）

```
┌──────────────────────────── 浏览器 ────────────────────────────┐
│  editor.js  (页面控制器，基本不变)                              │
│  adapter.ts (Matrix Adapter，改造为 CRDT 驱动)                  │
│     ├─ Y.Doc(project)  ← 新增，单一数据源                       │
│     │    └─ rows_<sheet>: Y.Array<Y.Map>                        │
│     ├─ 绑定: Y.Array ⇄ Univer worksheet                         │
│     │    · setSheetData ← 从 Y.Array 投影                       │
│     │    · 本地编辑/粘贴/填充 → 写 Y.Map(事务)                   │
│     │    · observeDeep → 增量重绘变化的行/格                    │
│     ├─ 行操作: insert/delete/move/duplicate → Y.Array 事务      │
│     ├─ 视图态: 筛选/排序/查找 = Univer 本地(不改 CRDT)          │
│     └─ y-websocket provider + awareness(在编辑行/远端光标)      │
└───────────────▲────────────────────────────────────────────────┘
                │ WebSocket (Yjs 二进制 update / awareness)
                │ wss://host/collab/matrix/<project>?token=...
┌───────────────┴──────── 协同服务进程（新增, 独立） ─────────────┐
│  uvicorn + pycrdt-websocket (ASGI)   ── 或 Node y-websocket     │
│   ├─ Room = project(:sheet)                                    │
│   ├─ 内存 Y.Doc, relay 广播, awareness                          │
│   ├─ 鉴权: 校验 Flask 签发 collab-token(项目写权限)             │
│   └─ 持久化 & 物化:                                             │
│        · 追加 update → lm_collab_doc (PG)                       │
│        · 去抖(0.5~2s) 将 Y.Array → 物化 TestItemRow             │
│          (uuid upsert / 删除 / row_order=下标 / 回写 id,version)│
└───────────────▲────────────────────────────────────────────────┘
                │ 复用 SQLAlchemy 模型 / items_service
┌───────────────┴─────────────── Flask (waitress, 不变) ──────────┐
│  /collab/token   签发校验(复用 permissions)                     │
│  items/matrix API 保持可用(非协同/降级路径)                     │
│  Excel 导入导出保持不变；导入=向 Y.Array 批量插入(见 §7)         │
│  PostgreSQL: 新增 collab 表；沿用 TestItemRow(uuid/version)      │
└─────────────────────────────────────────────────────────────────┘
```

要点：
- **Flask/waitress 不动**；协同进程独立，宕机则前端**优雅降级**回“逐行 PATCH + version 冲突刷新”（即现状），功能不丢。
- **物化保证**：DB 始终是权威结构化数据；runner/export/Excel 导出读 DB，**零改动**。

---

## 5. 传输层与库选型

### 5.1 传输：为何独立进程
waitress 同步 WSGI **不支持** WebSocket 升级。可选：
- **A（推荐）ASGI 独立进程**：`uvicorn` 跑 `pycrdt-websocket`（Python，同栈，可直接 `import app` 复用 `permissions`/`items_service` 做物化）。
- **B** Node `y-websocket` 官方服务：生态最稳，但物化要回调 Flask(HTTP) 或直连 PG，多一门语言运维。
- C 把 Flask 换 ASGI(Quart)：改动面大，**不推荐**。

### 5.2 CRDT 库
| 位置 | 选型 |
|---|---|
| 前端 | `yjs` + `y-websocket`(client) + `y-protocols`(awareness) |
| 服务端(推荐) | **`pycrdt` + `pycrdt-websocket`**（原 ypy 后继，协议兼容 y-websocket） |
| 服务端(备选) | Node `y-websocket` server |

### 5.3 前端离线构建（项目强约束：全离线 vendor，无 CDN）
- `yjs/y-websocket/y-protocols` 进 `frontend/package.json`，vite 打成 `vendor/collab/collab.umd.js`（`window.LMCollab`），与 Univer 同套离线构建。
- **需预置离线 npm 包**：当前沙箱 `/app/workspace/pkgs` 只有 Python wheel，需另行准备这些 npm 包（有网环境或内网 registry）。

---

## 6. Matrix Adapter 改造（adapter.ts / editor.js）

现有 `adapter.ts` 已具备干净的“行↔worksheet”结构，改造是**替换数据源**而非重写：

1. **数据源切换**：`SheetCtx.items` 从「本地数组」变为「`Y.Array` 的投影缓存」。
   - `setSheetData` 初次由 `Y.Array` 内容渲染；此后由 `Y.Array.observeDeep` 增量刷新（只重绘变化的行/格，保住大表性能）。
2. **本地编辑落点变更**：`_flushSync` 里原来 `onSave(PATCH)` 改为 **在一个 Yjs 事务里 set 对应 `Y.Map.fields[key]`**。
   - 粘贴/拖拽填充/多格编辑 = 一个事务批量 set（CRDT 合并友好）。
   - `steps`/只读列仍走 revert 逻辑（不可手改）。
3. **行操作 → `Y.Array` 事务**：
   - 插入行：新建 `Y.Map{uuid,id:null,fields:{默认值}}`，`insert(index)`。
   - 删除/批量删除：`Y.Array.delete(index,len)`。
   - 上下移/块移动：`Y.Array` 删除+插入（保序移动）。
   - 复制行：深拷贝 `Y.Map`（换新 uuid、id:null）插入。
4. **视图态与协同解耦（关键）**：Univer 的**筛选/排序/查找替换**是**本地视图**操作，**绝不**触发 `Y.Array` 重排；只有显式“移动行”才改 CRDT 顺序。避免“别人一排序全员乱序”。
5. **presence/awareness**：把 Univer 选区（行/格）写入 awareness；渲染他人“正在编辑此行”的行高亮/头像、远端光标（可复用 crosshair 高亮色）。**实现细节见 §6.1。**
6. **优雅降级**：`LMCollab.connect` 超时/无权限 → adapter 回退到**现状 PATCH 模式**（保留 `onSave` 及 `VERSION_CONFLICT` 处理）；顶部提示“实时协同暂不可用”。
7. **editor.js**：基本不变，仅在 mount 时决定「协同模式/降级模式」，并把 `onInsert/onDelete/onMove/...` 在协同模式下改指到 adapter 的 `Y.Array` 事务封装。

---

### 6.1 共享光标与在线存在（Awareness）

**能力确认**：共享光标位置 + 用户存在信息（谁在线 / 在编辑哪一行）由 Yjs 原生的 **Awareness 协议**（`y-protocols/awareness`）实现，**无需额外服务或组件**，也不改传输/服务端选型。

**为什么“几乎免费”**：
- Awareness 是**独立于文档内容的易失状态通道**：每客户端广播一小段“我是谁、我在哪”，**不进 CRDT、不落库、断线自动过期**（关页/掉线 → 该用户光标与头像自动消失，无需写下线清理）。
- 传输**复用同一条 WebSocket**：`y-websocket`(client) 与 `pycrdt-websocket`(server) 均**内置转发 awareness**，**服务端零额外开发**。

**本地 state 结构（各端 `setLocalState`）**：
```js
provider.awareness.setLocalState({
  user:      { id, name, color },            // 存在信息：谁在线（color 由服务端按 user_id 稳定分配，保证同人各端同色）
  cursor:    { sheet: 'test', uuid, col },   // 光标：定位到“行身份 uuid + 列”，而非绝对行号
  selection: { sheet, uuid, c1, c2 }         // 可选：选区范围
});
```
> **关键：光标以行 `uuid` 定位，不用绝对行号。** 因为他人可能处于**筛选/排序后的视图**（§1.4 与 §6 第 4 点的“视图态解耦 / 视图行 ↔ uuid 映射”），必须按 uuid 换算回本端当前视图坐标，否则不同排序下光标会画错格。

**渲染（各端监听 `awareness.on('change')` → `awareness.getStates()`）**：
- **在线成员列表**：按 `user.id` 去重，展示头像 + 用户色。难度低（纯读取）。
- **正在编辑此行**：把该用户 `cursor.uuid` 对应的**整行**用其 `user.color` 高亮。难度低-中。
- **远端光标/选区**：在 Univer 之上叠一层**绝对定位 overlay**（div 或 Univer 浮层/批注 API），按 uuid→当前视图行列换算坐标绘制带用户色的光标框。**这是唯一有工作量处**——社区版无现成“远端光标”UI，需自绘；坐标换算复用 adapter 已跑通的行列定位逻辑。

**身份来源**：`/collab/token`（§8）已含 `user_id`；前端连上后把 `user.id/name/color` 写入 awareness。颜色建议**服务端稳定分配**（如 `hash(user_id)` 取色板）。

**难度与落位**：

| 能力 | 难度 | 阶段 |
|---|---|---|
| 在线成员列表（谁在线 + 头像） | 低（纯 awareness 读取） | Phase 1 收尾可做 |
| 正在编辑行高亮 | 低-中 | Phase 2 |
| 远端光标/选区精确渲染 | 中（自绘 overlay + uuid→视图坐标映射） | Phase 2 |

---

## 7. 服务端物化与 Excel 导入导出

### 7.1 去抖物化（Y.Array → TestItemRow）
每 room 去抖 0.5~2s，无新编辑后执行一次事务：
1. 读 `Y.Array` 全量 `{uuid,id,fields,顺序}`。
2. **对账**：
   - `uuid` 已存在 → `update_item`（仅变化字段），`row_order = 数组下标`，`version+1`，回写 `Y.Map.id/version`。
   - `uuid` 新 → `create_item`，分配 `id/version` 回写 `Y.Map`。
   - DB 有、`Y.Array` 无 → `soft_delete_item`。
3. 校验（复用 `validation`）：阻断级错误不落库该行，通过 awareness/错误通道回报（单元格标红），**不影响其他行**。
4. 审计：物化者身份取该行最后编辑的协同用户（awareness 携带）；或系统用户 + 参与者列表。

### 7.2 快照 & 压缩 + PG-backed YStore 骨架（据 `BaseYStore` 自省实测）
- `lm_collab_doc` 追加 update；`lm_collab_snapshot` 存合并态，定期 compaction 截断旧 update，降低进房重放成本。
- **`BaseYStore` 抽象面（自省确认）**：子类必须实现 **`read()`（异步迭代已存 `(update, metadata, timestamp)`）** 与 **`write(data: bytes)`（追加一条 update）**；生命周期由基类的 `start()/stop()`（`started`/`stopped` 事件 + `_task_group`）驱动；`apply_updates(ydoc)` 基类默认实现会遍历 `read()` 把 update 灌进 Doc（可覆盖以走 checkpoint 加速）；`encode_state_as_update` / `get_metadata` 可选覆盖。
- **PgYStore 骨架**（每 project 一个 `path`，复用 Flask 的 SQLAlchemy engine；写用**同步 session 包进 `anyio.to_thread`** 避免阻塞事件循环）：

```python
# run_collab 侧：app/collab/pg_ystore.py
from pycrdt.store import BaseYStore, YDocNotFound
from pycrdt import Doc, merge_updates
import anyio, time

class PgYStore(BaseYStore):
    """把 Y update 追加到 lm_collab_doc(project_id, seq, update, metadata, ts)。
    path == f'project:{project_id}'，一库多文档，与 SQLiteYStore 同构。"""

    def __init__(self, path, metadata_callback=None, log=None):
        self.path = path                     # 'project:{id}'
        self.metadata_callback = metadata_callback
        self.log = log
        self._pid = int(path.split(":")[1])

    async def start(self, *, task_status=None, from_context_manager=False):
        # 无需建表（Flask 迁移已建）；置 started 事件即可
        await super().start(task_status=task_status,
                            from_context_manager=from_context_manager) \
            if False else self.started.set()   # 见下方“生命周期”说明

    async def read(self):
        rows = await anyio.to_thread.run_sync(self._read_sync)
        if not rows:
            raise YDocNotFound
        for update, metadata, ts in rows:
            yield update, metadata, ts

    async def write(self, data: bytes):
        md = b""
        if self.metadata_callback:
            md = await _maybe_await(self.metadata_callback())
        await anyio.to_thread.run_sync(self._write_sync, data, md, time.time())

    # —— 同步 DB 部分：在 Flask app_context 里用现有 db.session ——
    def _read_sync(self):
        with flask_app.app_context():
            rows = (CollabDoc.query
                    .filter_by(project_id=self._pid)
                    .order_by(CollabDoc.seq.asc()).all())
            return [(r.update, r.metadata or b"", r.ts) for r in rows]

    def _write_sync(self, data, metadata, ts):
        with flask_app.app_context():
            seq = (db.session.query(db.func.max(CollabDoc.seq))
                   .filter_by(project_id=self._pid).scalar() or 0) + 1
            db.session.add(CollabDoc(project_id=self._pid, seq=seq,
                                     update=data, metadata=metadata, ts=ts))
            db.session.commit()

    # compaction：把 N 条 update 合成 1 条快照，截断旧行
    def compact_sync(self):
        with flask_app.app_context():
            rows = CollabDoc.query.filter_by(project_id=self._pid) \
                     .order_by(CollabDoc.seq.asc()).all()
            if len(rows) < COMPACT_THRESHOLD:
                return
            merged = merge_updates(*[r.update for r in rows])
            for r in rows: db.session.delete(r)
            db.session.add(CollabDoc(project_id=self._pid, seq=1,
                                     update=merged, metadata=b"", ts=time.time()))
            db.session.commit()
```
> **生命周期说明**：`BaseYStore.start()` 自省显示它管理 `started/stopped` 事件与内部 `_task_group`。PgYStore 无异步建表工作，最简做法是**不覆盖 `start()`**、直接用基类实现（它会 set `started`）；只有需要后台压缩任务时才在 `start()` 里 `task_group.start_soon(self._compact_loop)`。上面 `start()` 的写法仅示意“无额外初始化”，落地时删掉该覆盖、由基类接管即可。
- **CollabDoc 模型**（Flask 迁移新增）：`(id, project_id FK, seq int, update LargeBinary, metadata LargeBinary null, ts float, created_at)`，唯一索引 `(project_id, seq)`。这就是 y-leveldb / y-redis-persistence 在本方案中的等价物（PG 为权威，见 §5.2 规则）。

### 7.3 Excel 导入/导出
- **导出**：读 DB 行（物化结果），**完全不变**。
- **导入**：解析 Excel → 在**一个 `Y.Array` 事务**里批量插入/更新 `Y.Map`（而非直接写 DB），使导入结果实时出现在所有协同者屏幕；随后由去抖物化落库。
  - 若导入在协同关闭态发生，走原 DB 路径即可（降级兼容）。

---

## 8. 认证与授权

1. 用户已登录 Flask（现有 session cookie）。
2. 开协同前调 **`GET /collab/token?project=<id>`**（Flask）：校验登录 + 该 project **写权限**（复用 `permissions.py`）→ 返回**短期签名 token**（itsdangerous/JWT，含 user_id、room、exp≈5min）。
3. 前端 WS 携带 token；协同进程用**同一 `SECRET_KEY`**校验，绑定 user 到 awareness，拒绝越权房间。
4. token 过期前端静默续签、不断连。只读成员可给“只读连接”（收更新、awareness 不可写、拒绝物化其编辑）。

---

## 9. 分阶段落地

**Phase 0 — 打底（无行为变化）**
- [ ] `frontend/` 引入 `yjs/y-websocket/y-protocols`，备离线 npm 包，vite 出 `collab.umd.js`(`window.LMCollab`)。
- [ ] Flask `/collab/token` 路由（复用权限）。
- [ ] 新增 PG 表 + 迁移。

**Phase 1 — Matrix 行级协同（MVP，核心）**
- [ ] 独立协同进程（pycrdt-websocket + PG store + 去抖物化对账）。
- [ ] `sheet → Y.Array<Y.Map>` 模型 + `uuid` 身份 + id 回写。
- [ ] `adapter.ts`：数据源切 Y.Array，`_flushSync`→Yjs 事务，`observeDeep`→增量重绘。
- [ ] 行操作(insert/delete/move/duplicate) → Y.Array 事务。
- [ ] 视图态(筛选/排序/查找) 与 CRDT 解耦。
- [ ] 优雅降级路径（协同不可用→现状 PATCH）。

**Phase 2 — 体验增强**
- [ ] awareness（见 §6.1）：在线成员列表（Phase 1 收尾可提前）、正在编辑行高亮、远端光标/选区自绘 overlay；光标以行 `uuid` 定位并按视图坐标映射。
- [ ] Excel 导入走 Y.Array 批量事务。
- [ ] 断线重连、token 续签、大矩阵性能压测与虚拟化校验。
- [ ] 校验错误的单元格级回报（不阻断他行）。

**Phase 3 —（顺带）Steps 明细协同**
- [ ] 复用同套 collab 基座，把 steps JSON 升级为子文档级 CRDT（`Y.Array` 步骤 + `Y.Map` 字段）。可选，优先级低于 Matrix。

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 大矩阵（数万行）单 Doc 内存/带宽 | 卡顿 | 每 sheet 一个 Doc、按需加载；Univer 已虚拟化；snapshot+compaction |
| Univer 社区版事件/坐标 API 不足以做双向绑定 | 绑定受阻 | 复用现有 `_flushSync`/`setSheetData` 已跑通的坐标逻辑；缺口用内置 FallbackGrid 兜底 |
| 筛选/排序误改 CRDT 顺序 | 全员乱序 | 明确“视图态不进 CRDT”，仅显式移动行改 `Y.Array` |
| 新行 id 未定引用 | 数据错乱 | `uuid` 作 CRDT 身份，服务端物化回写 id/version |
| 物化与外部写(Excel/脚本) version 漂移 | 保存失败 | 协同期视 CRDT 为权威，重读 version 重试并回写 |
| 前端离线 npm 包缺失（沙箱仅 py wheel） | 无法构建 collab bundle | 提前在有网/内网 registry 备包；文档已标注清单需求 |
| 多协同实例房间不一致 | 数据分叉 | 一期单实例；扩展再引入房间粘性路由 + Redis 广播 |
| waitress 无法承载 WS | —— | 已定：WS 走独立 uvicorn 进程 |
| 审计粒度变粗（去抖批量物化） | 追溯性 | awareness 记参与者；必要时以 CRDT update 日志作细粒度审计源 |
| Univer bundle 当前为旧版(缺多 sheet API) | 主表退回内置表格 | 需先 `frontend/ npm install && npm run build` 恢复 Univer（既有遗留项，独立于本设计） |

---

## 11. 待你拍板的开放问题

> **已拍板（2026-07-20）**
> - **#1 服务端语言 = Python**（`pycrdt` + `pycrdt-websocket` + uvicorn 独立进程）。理由：物化对账必须复用现有 SQLAlchemy 模型 / `silver_json_export` / runner，同栈直接 import，避免跨语言重写导出与校验逻辑。
> - **#2 Doc 粒度 = 每 project 一个 Doc**（含该 project 的全部 sheet：test/const/lib，各为一个 `Y.Array<Y.Map>`）。理由：矩阵行数在数千内，单 Doc 让跨 sheet 复制粘贴 / 整表操作 / presence 实现最简，最贴合“Matrix 是一个整体工作面”。行数若日后突破约 2 万再降级 per-sheet（见 §10 风险行）。

3. **同格并发策略**：字段级 LWW 是否够用，还是要“单元格锁/编辑指示”避免互相覆盖？
4. **presence 深度**：MVP 只要“内容实时合并”，还是一并要在编辑行高亮/远端光标（Phase 2）？
5. **多实例扩展**：近期是否需要协同服务水平扩展（影响是否一期引入 Redis/粘性路由）？
6. **物化频率/审计**：去抖间隔与审计记录方式（系统用户 vs 最后编辑者 vs 全参与者）？
7. **离线 npm**：构建环境能否联网装 `yjs` 等，还是需要我列出精确离线包清单？
8. **Steps 协同优先级**：确认放到 Phase 3（顺带），本期专注 Matrix？

---

## 12. 现有代码架构适配性评估（基于源码勘察）

**总评：高度适合。** 这是“加一层旁路”而非“重构”——主链路（Flask/waitress/DB/runner/export）几乎不动，Yjs 作为旁路接入。三个已具备的前提：行=item 且**已有 `uuid` 列**、服务层与请求上下文解耦、前端 adapter 为回调契约式绑定。

### 12.1 🟢 天然契合（可直接复用）

| 已核实的现状事实 | 对协同的意义 |
|---|---|
| `create_app()` 工厂被 web+worker 共享（`app/__init__.py`） | 新进程 `run_collab.py` 同样 `create_app()` 即可拿到 db/models/services，物化零重写。Python 单栈决策的根基。 |
| `items_service` 是纯服务函数（`create_item/update_item/soft_delete_item/move_items/bulk_*`），只依赖 `db.session`+model，不碰 request | 物化对账**直接调这些函数**，业务规则（字段默认值、`case_id` 自动、`row_order` 规整、唯一校验）全复用。 |
| `TestItemRow` 已有 `uuid`(32)+`row_order`+`version`+`sheet`+`custom_values`(JSON) | CRDT 四要素齐备：行身份(`uuid`)、顺序(`row_order`=数组下标)、物化回写(`version`)、分表(`sheet`)。**无需加列。** |
| `permissions.can()` 是纯函数 + `users_service.role_in_project(pid,user)` | `/collab/token` 校验 = `can("item.edit", role_in_project(pid,user))`；协同进程离线鉴权复用同一 `can()`。 |
| Flask 签名 cookie session + `SECRET_KEY`（`config.py`） | token 用**同一 `SECRET_KEY`** 签发/校验（itsdangerous/JWT HS256），协同进程无需连 session 存储。 |
| 已有 SSE 长连接（`text/event-stream`+keep-alive，`lanmatrix_api.py`） | 团队已接受长连接+线程模型；前端已有“服务器推”的消费惯例。 |
| adapter.ts 回调契约（`onSave/onInsert/onDelete/onMove/onBulkDelete/onBulkDuplicate` + `applying` 重入锁 + `VERSION_CONFLICT` 处理） | 协同改造 = 把回调**实现体**从 PATCH 换成 Y.Array 事务，接口面不变；`applying` 锁复用来防 observe 回环；**降级 = 保留原实现体**。 |

> 一句话：现有 `items_service` + `uuid` + 回调式 adapter，几乎就是“为 CRDT 物化预留的接口”。

### 12.2 🟡 需要适配（有工作量，方向清晰）

1. **传输旁路**：waitress 同步 WSGI 不支持 WebSocket 升级（SSE 能撑是“一线程一流”）。→ 独立 `uvicorn+pycrdt-websocket` 进程，成为第 3 个进程（web/worker/collab），反代加 `Upgrade` 透传。
2. **物化事务边界**（**最实打实的改造点**）：现每个 `items_service` 操作**自带 `db.session.commit()`**（逐操作提交）。物化是批量对账，需要：① 独立 scoped session（不与 web 请求共享）；② 一次去抖对账的多行 upsert/删除/重排包进**一个事务**；③ 给服务函数加 `commit=False` 变体，或物化层自管 `db.session`（更干净）。
3. **乐观锁让位 CRDT**：`update_item` 现在 `version != item.version` 直接抛 `VersionConflict`。协同期物化是唯一写者、CRDT 权威 → 需**物化专用 update 变体**：读最新 version、覆盖、自愈，不抛冲突。
4. **adapter 数据源切换**：`SheetCtx.items` 本地数组 → Y.Array 投影；`_flushSync` per-row PATCH → Yjs 事务；新增 `observeDeep` 增量重绘。集中在一个文件，但**需有网机器 `npm run build`**。
5. **JSON 动态列建模**：`custom_values`/`tags` 在 CRDT 里**推荐拍平进行 `Y.Map`**（字段级合并），物化时组回 `custom_values` JSON。

### 12.3 🔴 风险 / 摩擦点（必须正视）

1. **双写权威冲突（最需纪律）**：协同开启后 DB 有两个写入者——物化 vs 现有 REST（Excel 导入、后台脚本、降级 PATCH）。同一 project 同时被两路写，`version` 会打架。**对策 = 单一写者边界**：project 进入协同态时，REST 写路径要么走同一 Y.Doc（导入→Y.Array 批量事务），要么被短暂拒绝/引导。
2. **`move_items` 全表重排**（规整 `row_order` 为 1..N）：协同下顺序权威在 Y.Array，DB `row_order` 变成**物化产物**。必须保证**无任何非物化路径再调 `move_items`** 改顺序。
3. **进程 DB 连接压力**：多一常驻进程 = 多一组 PG 连接池（现 waitress `threads>=16`、worker 16、同库）。协同进程需**限制连接数**、复用 scoped session，勿打爆 PG。
4. **Univer 旧 bundle 前置阻塞**：多 sheet API 未 rebuild，主表当前退回 FallbackGrid。adapter 协同改造依赖 Univer 正常工作 → **必须先在有网机器重建 `frontend/`**，否则协同也只能跑在 FallbackGrid 上。

### 12.4 落地形态与设计纪律

```text
create_app()  ← 三进程共用
   ├─ run_web.py     (waitress, WSGI)      REST/SSE/页面，不变
   ├─ run_worker.py  (huey)                任务执行，不变
   └─ run_collab.py  (uvicorn+pycrdt-ws)   ← 新增，旁路
         ├─ 复用 items_service（物化）
         ├─ 复用 permissions.can / role_in_project（鉴权）
         ├─ 独立 scoped session + 批量事务 + 自愈 update 变体
         └─ lm_collab_doc(PG bytea) 快照/增量
```

**纪律（务必遵守）**：
- **单一写者**：project 协同态下，顺序与内容权威在 Y.Doc；DB 是物化产物。
- **服务层加 `commit` 开关 / 物化层自管事务**——唯一需动 `items_service` 处，且是**加参数不是改逻辑**，向后兼容。
- **降级 = 回退现有 PATCH 实现体**，接口不变，故降级“免费”。
- **前置阻塞**：先重建 Univer bundle，再动 adapter 协同改造。

---

## 13. API 依据（版本锁定与关键调用）

### 13.1 锁定版本（已确认）
| 组件 | 版本 | 状态 |
|---|---|---|
| `pycrdt` | **0.14.1** | ✅ 已在目标平台成功安装（Rust 轮子有 wheel，Python≥3.10）——**「Python 单栈」方案的最大风险点已消除** |
| `pycrdt-websocket` | **0.16.4**（依赖 pycrdt 0.14.x） | ✅ 已装并自省确认 |
| `anyio` | 4.14.2 | ✅（pycrdt-websocket 依赖） |
| `uvicorn` | 已装 | ✅ ASGI 宿主 |
| 前端 `yjs` / `y-websocket` / `y-protocols` | 待锁定（建议最新稳定） | 二进制协议与 pycrdt 兼容 |

> **§13.2–13.4 已用自省脚本（`inspect_pycrdt.py`）对照实际安装版本核实，非凭记忆。** 实弹冒烟通过：`get_update()` 出 58 字节增量、另一 Doc `apply_update()` 还原成功、`ArrayEvent` 捕获成功。

### 13.2 pycrdt 0.14.1 关键 API（自省实测）
```python
from pycrdt import Doc, Array, Map

doc = Doc()
doc["rows_test"] = rows = Array()               # 顶层共享类型：以 key 绑定到 Doc
row = Map({"uuid": u, "id": None, "case_id": ""})  # 预备(prelim) Map，可带初始 dict
with doc.transaction(origin="collab"):          # 事务：同一时刻仅一个；context manager，可带 origin
    rows.append(row)                            # insert(index, object) 收【单个】对象；另有 extend/pop/clear
    # 删除：del rows[i]（或 rows.pop(i)）；无 rows.move —— 见下
# 字段写入：ymap 用 dict 风格
with doc.transaction():
    row["result"] = "PASS"                      # Map 无 .set()，用 row[key]=v 或 row.update({...})

# —— 观察（物化触发点）——
sub_doc  = doc.observe(on_txn)     # 收 TransactionEvent，每次事务一次 → 最适合挂【去抖物化】
sub_arr  = rows.observe(on_arr)    # 收 ArrayEvent，属性 = delta / path / target / transaction
sub_deep = rows.observe_deep(on_deep)  # 收 list[BaseEvent]（含嵌套 Map 变更）→ 适合增量重绘
# 事件里的 delta 即 Yjs delta 格式；退订：sub.drop() / rows.unobserve(sub)

# —— 增量协议（与 y-websocket 二进制兼容）——
sv     = doc.get_state()           # state vector（bytes）
update = doc.get_update(sv)        # 自 sv 起的增量；get_update() 不带参 = 全量
doc.apply_update(update)           # 应用远端/存储的 update
merged = merge_updates(u1, u2)     # compaction：合并多个 update（模块级函数）
```

**确定的实现结论**：
- **`Array` 无 `move`（实测 `hasattr=False`）** → `onMove`/拖拽调序 = **`del rows[i]` + `rows.insert(j, obj)` 放在同一事务**。
- **`Array.insert(index, object)` 收单个对象**（非列表，与 Yjs JS 的 `insert(i,[...])` 不同）；批量用 `extend([...])`。
- **`Map` 无 `.set()`** → 字段写用 `row[key]=v` 或 `row.update({...})`。
- **物化去抖挂 `doc.observe(TransactionEvent)`**：整个 Doc 每次事务回调一次，最省心；`observe_deep` 留给前端增量重绘。
- **`origin` 参数存在** → 前端本地写用 `origin="local"`，`observe` 里据 `event.transaction` 的 origin 过滤，防回绘回环。
- **`Awareness` 类内建于 pycrdt 本体**（+ `create_awareness_message` / `is_awareness_disconnect_message`）→ presence/共享光标**服务端原生支持**，无需另接 y-protocols 服务端。

### 13.3 pycrdt-websocket 0.16.4 关键 API（自省实测）
```python
from pycrdt.websocket import WebsocketServer, ASGIServer, YRoom
from pycrdt.store import BaseYStore     # 子类化它做 PG 持久化（旁有 SQLiteYStore/FileYStore 参考）

server = WebsocketServer(
    auto_clean_rooms=True,              # 房间空闲自动回收 —— 生命周期不用自管
)
# 按 project 取/建房间；注入我们自己的 Doc 与 PG store
room = await server.get_room(f"project:{pid}")   # get_room / get_room_name / delete_room / rename_room
# 或自建： YRoom(ydoc=my_doc, ystore=PgYStore(pid))  →  server.start_room(room)

app = ASGIServer(
    server,
    on_connect=authorize,   # ★ 鉴权钩子：on_connect(scope) -> bool，返 False 拒绝连接
    on_disconnect=cleanup,
)
# uvicorn app  →  独立进程；Flask/waitress 不变
```

**确定的实现结论**：
- **鉴权 = `ASGIServer(on_connect=...)`**：`on_connect(scope) -> bool`（可 async），从 `scope` 取 URL 里的 token，用 Flask `SECRET_KEY` 校验 + `permissions.can(...)`，返 `True/False`。**无需自写 middleware。**
- **房间 = `WebsocketServer.get_room("project:{id}")`**；`auto_clean_rooms=True` 处理空闲回收。
- **每房注入 `YRoom(ydoc=..., ystore=...)`**：`ydoc` 上挂 `doc.observe` 做去抖物化；`ystore` 落 `lm_collab_doc`。
- **PG 持久化 = 子类 `pycrdt.store.BaseYStore`**：参照 `SQLiteYStore` 实现（存 update 序列 + `merge_updates` compaction）。抽象面已自省确认——实现 `read()`（异步迭代 `(update,metadata,ts)`）+ `write(data)`，生命周期由基类接管。**完整 PG store 骨架见 §7.2。**
- **presence**：`YRoom.send_server_awareness` + pycrdt 内建 `Awareness` 转发客户端光标/在线状态。

### 13.4 开放问题结论（原 4 点已全部解决）
| # | 问题 | 结论（自省实测） |
|---|---|---|
| 1 | `Array` 有无 `move` | **无** → delete+insert 同事务 |
| 2 | observe 回调/事件字段 | `ArrayEvent{delta,path,target,transaction}`；`observe_deep`→`list[BaseEvent]`；**`Doc.observe`→`TransactionEvent`（物化挂这里）** |
| 3 | pycrdt-websocket 入口/持久化基类 | `WebsocketServer`+`ASGIServer(on_connect)`+`YRoom(ydoc,ystore)`；持久化子类 **`pycrdt.store.BaseYStore`** |
| 4 | 房间获取/生命周期 | `get_room("project:{id}")` + `auto_clean_rooms=True` |

**全部结案**：`BaseYStore` 抽象面已自省确认——子类实现 **`read()`**（异步迭代 `(update, metadata, timestamp)`）+ **`write(data: bytes)`**（追加 update），生命周期由基类 `start()/stop()`（`started`/`stopped` 事件 + `_task_group`）接管，`apply_updates(ydoc)` 可选覆盖走 checkpoint。PG store 骨架见 **§7.2**。至此 §13 无待办项。

---

*本文档仅为设计讨论，未改动任何源码。方向确认后可据此拆成可执行实施 checklist。*
