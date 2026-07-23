# Silver Test Platform

A web-based, streaming test-execution platform for Synopsys Silver, built for
**internal-network / offline** deployment. Select a test-case folder in the
browser, pick the test ids to run, watch the log and progress stream live, and
download the result report — all from a web page, no desktop client.

Pure-Python stack — **no Node.js, Redis, RabbitMQ or Docker required.**

```
Flask 3  +  SQLAlchemy 2 (PostgreSQL)  +  Huey (SqlHuey/PostgreSQL)  +  SSE  +  Bootstrap
Waitress (prod server)  +  Pydantic  +  pytest
```

## Features

- **Folder submission in the browser**: choose your test-case folder; the page
  discovers the test ids inside it (every folder containing a `judge.py`) and you
  tick the ones to run — no manual typing, no desktop client.
- **`lib` / `stdlib` folder upload**: optionally attach your `lib` and `stdlib`
  folders; their contents are used to rewrite each selected `judge.py` into a
  *self-contained* script (the local modules it imports are inlined/replaced),
  so the run needs no library folders on disk.
- **Admin-registered server-side `.sil` models**: an administrator registers one
  or more `.sil` **paths on the server** (not an upload). Testers pick one by
  name when submitting; the model is opened in place and never uploaded.
- **Token-gated admin page**: the admin console stays locked and loads no data
  until the correct `ADMIN_TOKEN` is entered and verified.
- Task queue with a runtime-adjustable **license/concurrency limit**.
- **Pre-warmed Silver instance pool** — the worker launches `LICENSE_LIMIT`
  empty Silver instances at start-up (each holding one license) and **reuses**
  them for every test instead of launching a new Silver process per run. This
  removes the slow Silver start-up from the critical path (a queued test just
  re-opens the model and reconfigures the modules) and **pre-empts the licenses**
  the moment the platform starts. The pool auto-resizes to the live license
  limit, and disabling it (`SILVER_POOL_ENABLED=0`) restores the classic
  one-process-per-test behaviour. See `app/runners/silver_pool.py`.
- **Realtime** log + progress streaming over Server-Sent Events (SSE).
- **Task list with filtering, sorting, and batch actions**: filter by
  status/submitter/text, sort by any column, and select multiple tasks to
  **download all their reports as one zip** or **cancel them in bulk**.
- Separate **Result** (judge verdict) column: a run can finish yet still carry
  a failing verdict parsed from `jdgrslt.log`, shown independently of the
  execution status. The parser understands the TestCaseCreator judge markers
  (`Step.N is passed/failed`, `Test is Passed`, `Test is failed in StepN`). A
  single `jdgrslt.log` may hold several test cases, so the parser first scopes
  to the task's own `Test case ID.<id> is started!` section — a passing test
  never inherits another case's failure. Only these explicit step/verdict
  markers count, so descriptive text never flips the result.
- Task detail shows a **Judge result** panel that renders `jdgrslt.log` with
  **failing steps highlighted** and a "failed steps only" filter.
- **Test Matrix (Excel import / export)** — an *additive* feature alongside the
  execution flow. Import an Excel test-requirement workbook (e.g.
  `VHILS1_RWS_MCU_Qualification_Test_SYS.xlsx`); the platform parses its summary
  `DB` table and per-category detail sheets into a **test summary table** and its
  **test items** (with procedure steps and input/expected signal headers), stores
  them in the database, and can **export the identical Excel layout** back out.
  The summary side and the detail side are regenerated together so both stay
  consistent (round-trip lossless). See `docs/TEST_MATRIX.md`.
- Duplicate-submission guard (double-clicks don't re-enqueue an active test).
- Admin page: register/remove `.sil` model paths, change the license limit,
  and manage every task with the same filter / sort / multi-select controls as
  the public task list, including **batch cancel** and **batch delete**.

## Architecture

```
Browser ──upload──▶ Flask (run_web.py) ──create Task──▶ PostgreSQL (app data)
                                     └──enqueue──▶ Huey queue (PostgreSQL)
                                                       │
Huey worker (run_worker.py) ◀────consume──────────────┘
   └─ borrow a pre-warmed Silver instance ─ run ─ return it ─ write task_events
Browser ◀── SSE stream (task_events) ── Flask
```

An **optional** third process (`run_collab.py`) adds real-time multi-user
editing to the LAN Test Matrix over WebSocket/CRDT; see *Real-time
collaboration* under **Run**. It is non-breaking — without it the editor uses
classic REST + polling.

**Pre-warmed pool.** On start-up the worker builds a `SilverInstancePool` of
`LICENSE_LIMIT` live Silver instances (each opened on the default registered
model, holding one license). Per test the worker *borrows* an idle instance,
calls `open(model)` + reconfigures the CsvWriter / judge modules (fast), runs,
and *returns* the instance — no process launch. The pool is also the
concurrency gate (at most `LICENSE_LIMIT` instances exist), and a background
reconciler grows/shrinks it when an admin changes the limit at runtime. A run
that is cancelled force-stops its instance, which the pool then disposes and
transparently recreates (re-grabbing the license). Set `SILVER_POOL_ENABLED=0`
to fall back to launching a dedicated instance per test (the DB license gate is
then used instead).

The web and worker processes share one **PostgreSQL** database, which is also
how realtime events flow between them — the worker appends rows to `task_events`
and the SSE endpoint replays them by id cursor. The Huey task queue is stored in
the same PostgreSQL server (`huey.contrib.sql_huey`), so no message broker and no
local SQLite file are involved.

## Install (offline)

Python 3.10+. On a machine with the wheels available:

```bash
pip install -r requirements.txt
```

For a fully offline install, pre-download the wheels on a connected machine
(`pip download -r requirements.txt -d wheels/`) and install with
`pip install --no-index --find-links wheels -r requirements.txt`.

## Run

**Recommended — one command starts everything** (web server + task worker):

```bash
python run.py
```

Open <http://localhost:8080>. `run.py` launches the worker as a managed child
process, so it "always runs" for as long as the server is up, and stops it
cleanly on Ctrl+C.

### Why is there a separate worker?

Submitting a task and *executing* it are split on purpose: the web app only
**enqueues** the task, and a **worker** picks it up and runs Silver. That way a
long-running test never blocks the web server or the live SSE log streams, and
you can run several tests concurrently (up to the license limit). `run.py`
manages both for you — you don't have to start two things by hand.

For scaled or service deployments you can still run them independently (e.g. the
worker on a different machine, or several workers):

```bash
python run_web.py        # web/API only
python run_worker.py     # one or more workers
```

Set `START_WORKER=0 python run.py` to start the web server only.

### Real-time collaboration (optional third process)

The LAN Test Matrix editor can run a **Yjs/CRDT real-time collaboration** layer
so several people edit the same matrix live (see
`docs/yjs-collab-design.md` / `docs/yjs-collab-checklist.md`). It is **fully
optional and non-breaking**: with the collab process (or its frontend bundle)
absent, the editor stays in classic REST + polling mode.

Waitress is a synchronous WSGI server and **cannot** perform the WebSocket
upgrade, so collaboration runs as its **own ASGI process** (`run_collab.py`,
uvicorn + `pycrdt-websocket`). It reuses the same app factory, database and
`SECRET_KEY`, so it verifies web-minted access tokens and materializes CRDT
changes back into `TestItemRow` through the existing service layer.

```
                      ┌───────────────────────── PostgreSQL (one DB) ─────────────────────────┐
Browser ─HTTP/SSE─▶ Flask (run_web.py, waitress/WSGI) ──REST/SSE──────────────────────────────┤
        └─WebSocket─▶ Collab (run_collab.py, uvicorn/ASGI) ──materialize / lm_collab_doc───────┤
                      Huey worker (run_worker.py) ──task_events─────────────────────────────────┘
```

Run the three processes side by side (all share the one PostgreSQL DB):

```bash
python run_web.py        # 1) web/API/SSE (waitress, default :8080)
python run_worker.py     # 2) task worker(s)
python run_collab.py     # 3) collaboration ASGI server (uvicorn, default :1234)
```

Install the collab-only dependencies (`pycrdt`, `pycrdt-websocket`, `uvicorn`,
`websockets`, …) on the collab host — they are pinned in `requirements.txt` and
**only this process needs them**. For a fully offline install, vendor their
wheels the same way as the base install (see *Install (offline)* above).

> ⚠️ **Always run the collab server with a single worker (`workers=1`).**
> Rooms (`Y.Doc`s) live in process memory; multiple OS workers would each hold a
> divergent copy of the document. `run_collab.py` hard-codes `workers=1`.
> Horizontal scale-out needs a Redis pub/sub relay and is out of scope.

**Reverse proxy.** Terminate TLS at your proxy and route the WebSocket path to
port 1234 with the `Upgrade`/`Connection` headers passed through. Example nginx:

```nginx
# WebSocket -> collab ASGI process (rooms are ws://host:1234/project:{id})
location /collab/ {
    proxy_pass         http://127.0.0.1:1234/;
    proxy_http_version 1.1;
    proxy_set_header   Upgrade    $http_upgrade;   # required for the WS upgrade
    proxy_set_header   Connection "upgrade";       # required for the WS upgrade
    proxy_set_header   Host       $host;
    proxy_read_timeout 3600s;                       # long-lived socket
}

# Everything else -> Flask/waitress web process
location / {
    proxy_pass       http://127.0.0.1:8080;
    proxy_set_header Host $host;
}
```

Then point the frontend at the proxied socket by setting **`COLLAB_WS_URL`**
(e.g. `wss://your-host/collab`) in the web process's environment; when unset the
frontend derives the socket URL from `window.location`.

**Single-writer boundary.** While a project has live collaborators the CRDT
materializer is its **only** authoritative DB writer. The collab server
heartbeats live-room presence into `lm_collab_presence`, and the web process
reads it to decide whether a project is "collaborative". Setting
**`COLLAB_REST_GUARD=1`** makes the web app reject direct REST row mutations on a
collaborative project (HTTP `409 COLLAB_ACTIVE`) so REST and CRDT never fight
over `row_order`/`version`. It is **off by default** (opt-in, backwards
compatible) and **fails open**: if the collab process crashes, its presence row
goes stale within `COLLAB_PRESENCE_TTL_SECONDS` and REST writes resume
automatically — the editor degrades gracefully to classic REST.

Collab-related environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `COLLAB_HOST` / `COLLAB_PORT` | `0.0.0.0` / `1234` | Bind address of `run_collab.py`. |
| `COLLAB_WS_URL` | *(derive from page)* | Explicit WS base handed to the browser (e.g. `wss://host/collab`). |
| `COLLAB_REST_GUARD` | `0` | Reject REST row writes on collaborative projects (single-writer boundary). |
| `COLLAB_PRESENCE_HEARTBEAT_SECONDS` | `10` | How often the collab server refreshes presence. |
| `COLLAB_PRESENCE_TTL_SECONDS` | `30` | How long a presence row stays "active" without a refresh. |
| `COLLAB_ROOM_IDLE_TTL_SECONDS` | `900` | Evict an idle (client-less) room after this long (`0` disables). |
| `COLLAB_ROOM_SWEEP_SECONDS` | `60` | Idle-room sweep interval. |

> **Frontend build.** The collaboration client bundle
> (`app/static/vendor/collab/collab.umd.js`) must be built on a networked
> machine with `cd frontend && npm install && npm run build` (the sandbox has no
> npm). If the bundle is absent the editor stays in classic mode.

Configuration is via environment variables (optionally a `.env` file); see
`.env.example`. Key settings: `RUNNER_BACKEND` (`mock` for demo, `silver` for
the real backend requiring `SILVER_HOME`), `LICENSE_LIMIT`, `ADMIN_TOKEN`,
`HOST`/`PORT`.

## Register `.sil` models (admin)

> **Since 2.12.0** administration lives inside lanmatrix. Log in as a **System
> Administrator** and open **管理台 → 模型管理** (`/lanmatrix/admin`). No
> `ADMIN_TOKEN` is required — authority comes from the account. The historic
> token-gated `/admin` page now redirects there. The description below is kept
> for context.

The plant model is a shared **server-side** asset referenced by path — it is not
uploaded. On the **管理台 → 模型管理** tab, add the absolute server path(s) of your
`.sil` file(s), each with an optional display name. Register several so testers
can choose which model to run against.

Each registered model shows a *present / missing* status reflecting whether the
file currently exists on the server. A demo model lives at `samples/model.sil`.

## Submitting tests (browser)

> **Since 2.12.0** submission is **per-project** and **members only**. Open a
> project and go to **上传任务** (`/lanmatrix/projects/<id>/tasks`). The submitter
> is taken from your logged-in account (no free-text name), and only members of
> the project may upload / run / download its tasks. The flow below is otherwise
> unchanged.

1. Open the project's **上传任务** page.
2. Pick a **Plant model (.sil)** from the registered list.
3. Under **Test-case folder**, select the folder that holds your test cases.
4. Optionally attach the library folders the judges import from:
   - **lib folder** — your `..\..\Lib` folder.
   - **stdlib folder** — your `02_Config\Library` folder (which contains
     `Lib`, `LibValue`, `StdLib`, `SystemVariable`).

   Their contents are used to inline (replace) each judge's local imports into a
   self-contained script server-side. **Nested sub-folders are searched
   recursively**, so a flat `from Common_Constant import *` resolves even when
   the module lives in `Library\Lib\`. If a judge imports a module that was not
   found in the uploaded folders, the submit result lists it as an "unresolved"
   note so you know which folder to add.
5. The page lists the discovered test ids (folders containing a `judge.py`).
   Tick the ones you want and click **Add selected to queue**.
6. The browser uploads the selected test cases plus the lib/stdlib folders; the
   server bundles each `judge.py`, runs it against the chosen model, and streams
   the log to **Tasks & history**.

Only the ticked test cases (plus lib/stdlib and sibling non-test-case files) are
uploaded; unticked test cases are left out.

Your selection (folders, model, ticked ids) is **remembered if you navigate to
another page and back** — the chosen files are stashed in the browser
(IndexedDB) and restored automatically, so you don't have to re-pick them. Click
**Start over** to clear it.

### Managing tasks (Tasks & history)

The task list supports **filtering** (status / submitter / free text) and
**sorting** (click any column header). Tick the checkboxes to select tasks, then
**Download selected reports** (returns one combined zip) or **Cancel selected**
(removes queued tasks instantly and stops running ones).

### Try the sample

Register `samples/model.sil` as a model (Admin), then on the home page select it,
choose the `samples/` folder, and tick `TC_SMOKE`.

## Bootstrap (optional)

The UI ships with a self-contained stylesheet (`app/static/css/app.css`) and is
fully usable offline as-is. To use the full Bootstrap 5 framework, drop
`bootstrap.min.css` into `app/static/vendor/`; it is loaded automatically if
present.

## API

| Method | Path | Purpose |
|-------|------|---------|
| POST | `/api/uploads` | Stage a bundle, report its test ids |
| POST | `/api/tasks` | Create a task from a staged upload |
| POST | `/api/tasks/upload` | One-shot upload + create |
| GET | `/api/tasks` | List tasks |
| GET | `/api/tasks/<key>` | Task status |
| GET | `/api/tasks/<key>/detail` | Task detail + events |
| GET | `/api/tasks/<key>/stream` | SSE live log/progress |
| POST | `/api/tasks/upload_tree` | Folder upload: stage tree (+lib/stdlib), queue selected ids |
| POST | `/api/tasks/<key>/cancel` | Cancel a task |
| POST | `/api/tasks/cancel_batch` | Cancel several tasks at once |
| GET | `/api/tasks/<key>/jdgrslt` | Judge result log (`jdgrslt.log`) as JSON text |
| GET | `/api/tasks/<key>/download` | Download the report zip |
| GET | `/api/tasks/download_batch` | Download several reports as one zip |
| GET | `/api/licenses` | License/concurrency status |
| GET | `/api/models` | Registered `.sil` model names (for pickers) |
| POST | `/api/admin/verify` | Verify the admin token (unlock the console) |
| GET | `/api/admin/models` | Registered models incl. paths (admin) |
| POST | `/api/admin/models` | Register a server-side `.sil` path (admin) |
| POST | `/api/admin/models/bulk` | Replace the whole model list (admin) |
| DELETE | `/api/admin/models` | Remove a registered model (admin) |
| POST | `/api/admin/license` | Change the license limit (admin) |
| POST | `/api/admin/tasks/<key>/cancel` | Cancel any task (admin) |
| POST | `/api/admin/tasks/cancel_batch` | Cancel several tasks (admin) |
| DELETE | `/api/admin/tasks/<key>` | Delete a task (admin) |
| POST | `/api/admin/tasks/delete_batch` | Delete several tasks (admin) |

## Tests

```bash
pytest                       # full suite (needs the deps installed)
python -m unittest tests.test_bundler   # judge bundler only (pure stdlib)
python tests/test_silver_pool.py        # instance-pool logic (pure stdlib)
```

`tests/test_silver_pool.py` verifies the pre-warmed pool (pre-warm, reuse,
concurrency cap, runtime resize, cancel/poison→replace, shutdown) with a
lightweight fake driver, so it runs without Flask or a real Silver install.

## Release notes

### 2.13.0
- **模板字段与 Test-Matrix 列对齐，移除兼容映射.** `TEMPLATE_FIELDS` 现按
  `matrix_excel.SUMMARY_COLUMNS` 顺序 1:1 镜像（仅排除工作簿计算列 `test_id`/`log`），
  补齐了此前缺失的 `test_name`（测试名）、`priority`（优先度，单选）、`result`（结果，
  单选，默认 Not Tested）、`remark`（备考），`exec_date`（实施日）改为 `date` 类型。
  `testmatrix_bridge.TM_TO_LM` 随之从「补丁式重映射（test_name→title、remark→comment）」
  简化为纯恒等映射；`priority`/`result` 仍透明落到首类列。**注意**：仅新建项目会 seed
  出新字段，存量项目无自动迁移。
- **关闭应用时优雅关停 Silver.** 组合启动器 `run.py` 在终止 worker 前，先把共享许可
  上限降到 0（`license_service.begin_drain()`），worker 的 reconcile 循环随即把 Silver
  连接池 target 收缩到 0、逐个 dispose 实例并释放许可，然后再终止进程并做兜底清理。
  下次启动时 `init_defaults` 会把被降为 0 的上限自动恢复为配置默认值。
- **步骤明细：入力値/期待値信号与手順列联动.** 步骤编辑器新增「添加信号 / 添加步骤」
  按钮；新增或重命名信号时，手順（步骤）表的对应列（`入力: <名>` / `期待: <名>`）会
  即时增列并重渲染，保持入力/期待与手順列的映射关系。
- **导入失败不再关闭对话框，按原因归类罗列每一行.** Test-Matrix 导入出现失败行时，
  对话框保持打开，在结果区**按相同原因归类**列出每行的 **行号 + case_id + 消息**，并给出
  一行「失败原因归类」汇总，方便一眼看出共同的失败原因。
- **修复 VHILS Excel「-」占位导致的解析/校验失败.** 当 項目作成=不要 时，実施日 /
  実施者 等单元格填 `-`（含全角/长音等变体）；导入时这些纯占位符统一按空值处理，
  不再触发日期/字段的「格式不正确」。
- **导入网格改为按テスト区分分页.** 仍保持单表形态，但不再按固定行数分页，而是把
  「テスト区分一致」的行归为同一页；翻页按钮在相邻区分之间跳转，并显示当前区分与行数。
- **新增 Ctrl+S / ⌘S 强制保存并同步.** 提交正在编辑的单元格（触发保存），等待在途
  PATCH 完成后重新加载，把其他人的最新修改一并拉取刷新。

### 2.12.0
- **管理台并入 lanmatrix，System Administrator 登录即用，彻底告别 `ADMIN_TOKEN`.**
  以系统管理员账户登录后，导航栏出现 **管理台**（`/lanmatrix/admin`）。旧的
  `/admin`、`/`（提交页）、`/tasks`（历史）路由改为 **302 重定向** 到 lanmatrix，
  既保留旧书签又堵住「任意登录用户可见全部任务」的越权。管理台含四个标签：
  - **账号管理**：以账号方式管理 Submitter/管理员（新建、改名/邮箱/密码、启用/禁用、
    授予/撤销系统管理员、删除）。内置守卫：不能停用/降级/删除**最后一名启用的系统
    管理员**，不能删除当前登录账户。
  - **模型管理**：注册/删除服务器端 `.sil` 模型（按路径打开，不上传文件）。
  - **授权 / 并发**：查看并设置并发上限。
  - **任务管理**：查看**所有项目**及历史遗留（未归属项目，`project_id` 为空）的任务，
    可取消/删除。
- **每个项目独立的「上传任务」页，只有项目成员可上传 / 运行 / 下载.** 任务
  （`tasks` 表）新增 `project_id`、`submitter_id`，归属到 lanmatrix 项目与账户。
  项目页新增 **上传任务**（`/lanmatrix/projects/<id>/tasks`），**完整覆盖原独立页面
  的全部能力**：浏览器内扫描测试用例文件夹、勾选 test id 提交（提交者取自登录账户）、
  任务列表经 **SSE** 实时刷新进度、支持取消/下载/删除；点击任务打开**详情弹窗** ——
  概览、**实时日志**（log/warning/error/progress/status/result 流，可自动滚动）、
  **判定结果查看**（jdgrslt.log，高亮失败步骤、可「仅显示失败步骤」、可刷新）。
  非成员访问只读拦截并提示。
- **项目成员管理.** 项目页新增 **成员管理**（`/lanmatrix/projects/<id>/members`）：
  搜索账户添加成员、调整角色（project_admin/editor/reviewer/reader）、移除成员；
  守卫「至少保留一名项目管理员」。RBAC：任一成员可 view/upload/cancel/download，
  仅 project_admin 可 delete；系统管理员全局放行。
- **接口**（均 session + CSRF，服务端 RBAC）：
  `GET/POST/PATCH/DELETE /projects/<id>/members[...]`、
  `GET /projects/<id>/members/candidates`、
  `GET /projects/<id>/tasks`、`POST /projects/<id>/tasks/upload-tree`、
  `GET /projects/<id>/tasks/<key>[/detail|/stream|/jdgrslt|/download]`、
  `POST /projects/<id>/tasks/<key>/cancel`、`DELETE /projects/<id>/tasks/<key>`；
  管理台 `GET/POST/PATCH/DELETE /admin/users[...]`、`/admin/models[/bulk]`、
  `/admin/license`、`/admin/tasks[/<key>/cancel]`（均 `system_admin` 强校验）。

### 2.11.0
- **无需 SQL 的可视化增删改查（数据库管理页新增「表管理」标签）.** 管理员进入
  `/lanmatrix/admin/db` 后，页面顶部提供「表管理 / SQL 控制台」两个标签：
  - **表管理**：左侧列出所有可管理的物理表（`app_settings`、`tasks`、`task_events`、
    `lm_*`、`kv`、`schedule`、`task` 等，含估计行数），点击即分页浏览该表数据。
  - **表格浏览**：分页（默认 50 行 / 页，最大 500）、点列头**排序**、显示主键列标记。
  - **表单增删改**：「新增行」弹出按列生成的表单（自增/默认值列标记为可选、布尔列识别
    `true/1/是/y` 等、空值按类型转为 `NULL` 或保留空串）；每行「编辑 / 删除」按主键定位。
  - **安全**：表名、列名一律基于**实时目录内省白名单**校验后再加引号拼接，所有取值以
    **绑定参数**传入，杜绝注入。**无主键的表（如 `kv`/`schedule`/`task`）仅支持浏览，
    表单编辑/删除不可用**——请改用 SQL 控制台。
  - **接口**（均 `system_admin` 服务端强校验）：`GET /admin/db/tables`、
    `GET /admin/db/tables/<table>/schema`、`GET /admin/db/tables/<table>/rows`、
    `POST /admin/db/tables/<table>/rows`、`PATCH /admin/db/tables/<table>/rows`、
    `DELETE /admin/db/tables/<table>/rows`。

### 2.10.1
- **修复：修改 `.env` 的 `LM_ADMIN_PASSWORD` 后管理员登录仍报「用户名或密码错误」.**
  过去引导管理员只在首次启动、且库中无管理员时创建一次，之后再改 `.env` 密码不生效。
  现在启动时会**对账**：只要设置了 `LM_ADMIN_PASSWORD`，每次启动都会把引导管理员
  （按 `LM_ADMIN_USER` 匹配，找不到则取任一系统管理员）的密码重设为该值，并清除锁定/
  禁用状态、置为 `active`；留空则永不覆盖已有密码。**改完 `.env` 重启应用即可恢复登录。**

### 2.10.0
- **系统管理员 PostgreSQL 管理页面.** 以管理员（`is_system_admin`）登录后，导航栏出现
  「数据库管理」入口（`/lanmatrix/admin/db`，非管理员访问自动跳回项目页）：
  - **连接概览**：后端/数据库名/连接用户/服务器地址端口/数据库大小/版本/服务器时间。
  - **数据表统计**：各用户表的模式、表名、估计行数、占用空间（按大小降序），一键「预览」
    前 100 行。
  - **SQL 控制台**：默认**只读模式**——仅允许 `SELECT/WITH/SHOW/EXPLAIN/TABLE/VALUES`，
    且在事务内执行后**回滚**，双重保险防止误写；关闭只读开关后为写模式，语句在独立事务中
    **提交**（前端二次确认 + 醒目告警）。结果最多 500 行、值做 JSON 安全转换、显示耗时。
  - **接口**（均 `system_admin` 服务端强校验）：`GET /api/v1/admin/db/overview`、
    `POST /api/v1/admin/db/query`（`{sql, read_only}`）。

### 2.9.0
- **Excel 级行操作（结构化矩阵，无需第三方电子表格引擎）.** 编辑器表格新增：
  - **多选**：行首复选框 + 表头全选；`Shift` 连选区间，点行号也可切换选中。
  - **指定位置插入行**：工具栏「上方插入 / 下方插入」，或右键菜单「在上方/下方插入行」，
    在所选（或右键指向）行的相邻位置插入空白草稿行。后端 `create_item` 支持 `anchor_id` +
    `place`（above/below），并对后续行 `row_order` 让位。
  - **复制多行 / 删除多行**：`POST /items/bulk-duplicate`、`POST /items/bulk-delete`，
    复制的副本按原顺序插入所选区块下方（复用单行校验/审计/`case_id` 去重）。
  - **上移 / 下移**：`POST /items/move`（整块移动并把 `row_order` 归一化为 1..N）。
  - **右键上下文菜单**：插入/复制/删除/上移/下移一站式操作；表格保留 `LMUniver.mount`
    钩子，未来可无缝切换到 Univer 引擎而不改数据契约。

### 2.8.0
- **全面 PostgreSQL（移除 SQLite）.** 平台数据库与后台任务队列现均只用 PostgreSQL：
  - **应用数据库**：`Config` 的 DB 段改为 PostgreSQL-only，`DATABASE_URL`（标准
    SQLAlchemy DSN，默认 `postgresql+psycopg2://postgres:postgres@localhost:5432/silvetestapp`）；
    删除 SQLite 分支、`DB_PATH`、WAL 连接参数与 `extensions.py` 里的 SQLite PRAGMA 监听器。
  - **任务队列**：`SqliteHuey` → `huey.contrib.sql_huey.SqlHuey`（peewee，PostgreSQL），
    与应用数据同库、无需 Redis/RabbitMQ 也无本地 SQLite 文件；测试/`HUEY_IMMEDIATE=1`
    时用内存队列（`MemoryHuey`）同步执行。可用 `HUEY_DATABASE_URL` 单独指定队列库。
  - **依赖**：新增必装 `psycopg2-binary`、`peewee`。
  - **备份/恢复**：`scripts/lm_backup.py` / `lm_restore.py` 移除 SQLite 分支，仅 `pg_dump` /
    `pg_restore`。
  - **测试**：`conftest.py` 改用 `TEST_DATABASE_URL`/`DATABASE_URL`（PostgreSQL），每例
    `drop_all`+建表隔离。
- **局域网用户自助注册.** 登录页新增「注册新账号」：
  - 新接口 `POST /api/v1/auth/register`（CSRF 豁免、纳入登录闸门白名单）与页面
    `/lanmatrix/register`。
  - 校验用户名白名单（`LM_USERNAME_PATTERN`）、密码最小长度（`LM_PASSWORD_MIN_LEN`）、
    用户名唯一性；新账号非管理员且无任何项目权限，需管理员分配角色后才能看到项目。
  - `LM_REGISTRATION_DEFAULT_STATUS=active` 时注册即登录；设为 `disabled` 则进入
    待审核状态，由管理员激活后方可登录。`LM_ALLOW_REGISTRATION=0` 可整体关闭。

### 2.7.5
- **将 LAN 测试矩阵从独立子包合并进 Silver 测试平台的常规分层.** 原先自成一体的
  `app/lanmatrix/` 被彻底拆解并入平台自身的代码分层，不再是“另一个项目”：
  - **模型** 迁移至 `app/models/lanmatrix.py`，并通过 `app/models/__init__.py`
    统一注册到平台唯一的 `db` 元数据（由主 `db.create_all()` 建表）。
  - **路由** 迁移至 `app/routes/lanmatrix_api.py`（`/api/v1`）与
    `app/routes/lanmatrix_pages.py`（`/lanmatrix`），蓝图名保持
    `lanmatrix_api` / `lanmatrix_pages` 不变。
  - **业务逻辑** 迁移至 `app/services/lanmatrix/` 子包（service / excel_service /
    testmatrix_bridge / audit / batch / validation / fields / repository /
    security / permissions / excel_io）。
  - **应用工厂** 直接注册上述蓝图，并将建表迁移（`lm_projects` 字段）与
    管理员种子（`_seed_lanmatrix_admin`）折叠进 `create_app`，删除旧的
    `lanmatrix.register()` 入口。
  - 模板 `app/templates/lanmatrix/*` 与静态资源 `app/static/js/lanmatrix/*`、
    `app/static/css/lanmatrix.css` 为命名空间资产，位置不变。
  - 旧 `app/lanmatrix/` 目录已删除；`url_for`、鉴权白名单、前端 API 路径均无变化。

### 2.7.4
- **精简默认字段、支持删除行、单元格按字段类型渲染编辑器.**
  - **默认字段只保留测试矩阵模板字段.** 新建项目仅初始化 17 个测试矩阵字段
    （`category`/`category_name`/`viewpoint`/`test_no`/`purpose`/`environment`/
    `env_version`/`data_flash`/`parameter`/`description`/`item_created`/`exec_date`/
    `executor`/`version_label`/`traceability_id`/`upper_req_id`/`steps`），不再额外
    植入旧的系统字段。`case_id`/`version`/`updated_at` 等内部列仍在行模型上透明工作
    （`case_id` 仍自动生成并保证唯一，用于行标识与去重）。
  - **支持删除行.** 表格每行「操作」列新增「删除行」按钮，确认后软删除该行并刷新
    （复用已有 `DELETE /projects/{id}/items/{item_id}`）。
  - **单元格按字段类型渲染对应编辑器.** 修复「设为单选型却无法选择」：`single_select`
    渲染下拉框（含空选项，选项取字段配置的 options），`multi_select` 渲染多选框，
    `boolean` 渲染是/否下拉，`date`/`datetime` 渲染日期选择器，其余类型仍为可编辑文本；
    修改即经乐观锁保存，服务端按数据类型强制校验/归一。未配置选项的单选字段回退为文本
    输入，避免无从填写。

### 2.7.3
- **合并 Test Matrix 与 Matrix Editor 为单一「Test Matrix」编辑平台，并移除「系统字段」概念.**
  - **删除独立的 Test Matrix 功能.** 移除旧版固定表结构的 Test Matrix 页面、蓝图、服务与
    模型（`routes/matrix_routes.py`、`services/matrix_service.py`、`models/test_matrix.py`
    及其模板/静态脚本），导航栏不再有两个入口——全站统一经 Matrix Editor 登录并进入，
    菜单项更名为「Test Matrix」。日文 Excel 导入/导出能力已由 `lanmatrix` 的
    `testmatrix_bridge` 承接（复用保留的 `services/matrix_excel.py` 编解码器），
    因此合并后功能不缺失。
  - **移除「系统字段」限制，所有字段均可修改/删除.** 新建项目的默认字段不再标记为系统字段
    （`is_system` 恒为 `False`）；字段配置页对每个字段都提供「编辑 / 启用停用 / 删除」，
    并允许修改数据类型（`data_type`）。`field_key` 仍不可变（它是存储/列路由标识），
    其余属性均可编辑。删除任一字段都会同步清理各测试项中的对应数据。
    列存字段（如 `case_id`/`title`）的底层列存储保持透明，改名或删除均安全。

### 2.7.2
- **图形化步骤明细编辑器 + 日文测试矩阵导入/导出.**
  - **每个测试项可图形化编辑步骤.** 表格每行新增「步骤明细」按钮，弹出可视化编辑器
    分三段维护 `steps` JSON：输入信号 `input_signals`、期望信号 `expected_signals`
    （名称/路径，可增删），以及步骤表 `steps`（手順番号/目的/操作/サブルーチン/引数/
    各输入·期望列/確認タイミング，可增删行；信号增减时步骤列自动同步）。保存时序列化为
    JSON 并经乐观锁 `PATCH` 写入该行 `steps` 字段，与 Test Matrix 步骤结构完全一致。
    （`app/static/js/lanmatrix/steps_editor.js`）
  - **按日文表头导入测试矩阵.** 导入对话框新增「格式」选择：`测试矩阵 (日文 VHILS 表头)`
    直接解析 `VHILS1_RWS_MCU_Qualification_Test_SYS.xlsx` 的汇总表与各分区明细表，
    按列映射到编辑器字段（`テスト名`→标题、`備考`→备注、`優先度` 高/中/低→High/Medium/Low、
    `結果` OK/NG→Pass/Fail，`手順` 明细→`steps` JSON），以 `case_id=前缀+区分+番号`
    做 upsert。`POST /projects/{id}/testmatrix/import`。
  - **导出为日文测试矩阵.** 「导出测试矩阵(日文)」按字段反向映射重建与源文件字节兼容的
    工作簿（汇总 `DB` 表 + 每分区明细表）；导入时捕获的 `id_prefix`/汇总表名存于项目
    （新增 `lm_projects.tm_id_prefix`/`tm_summary_sheet`，含幂等 `ALTER TABLE` 迁移）
    以保证往返一致。`GET /projects/{id}/testmatrix/export`。
  - 新增纯映射单元测试（导入/导出/枚举翻译/case_id/步骤解析）；已用真实工作簿验证
    导入→导出→再导入无损往返（6 项、含步骤，重建为 4 sheet）。

### 2.7.1
- **Matrix Editor fixes + Test-Matrix merge.**
  - **新增行不再报"输入数据校验失败".** 新增行现在创建一条*草稿行*：服务端自动
    生成 `case_id`、套用字段默认值，必填项可留空后在表格内逐格补全（`draft`
    模式跳过必填校验，但仍校验已填值的类型/范围/枚举）。同时修复清空 NOT NULL
    文本单元格会违反约束的隐患（`None`→`""`）。
  - **字段可修改与删除.** 字段配置页新增「编辑」（改显示名/选项/必填/提示）与
    「删除」（自定义字段，删除同时清除各测试项中该字段数据）；系统字段仍受保护。
    新增 `DELETE /projects/{id}/fields/{fid}` 接口。
  - **以 Test Matrix 字段为基础.** 每个新项目默认播种 Test Matrix 汇总表列
    （测试区分/观点/番号/目的/环境/实施者/追溯ID/上位要求ID… 及 `steps` 步骤明细
    JSON 字段），存于 `custom_values`，可按项目改名/增减/设为必填。
  - **全站统一登录.** 新增全局登录闸门：整站（原上传执行界面、Test Matrix 页面及
    其 API）统一走 Matrix Editor 登录，未登录页面 302 跳转 `/lanmatrix/login`
    （带 `next`），未登录 API 返回 401 JSON。可用 `GLOBAL_LOGIN_REQUIRED=0`
    关闭。导航栏新增「退出登录」。

### 2.7.0
- **LAN Test Matrix — offline online-editing platform.** New *additive*
  subpackage (`app/lanmatrix/`) adds a spreadsheet-style, fully-offline,
  LAN-deployable test-matrix editor at **`/lanmatrix`** (nav: *Matrix Editor*),
  built to `lan_test_matrix_prd.md`. Stack: **Univer Sheets (optional local
  drop-in) + openpyxl + PostgreSQL** — no commercial-license grid, no CDN. The
  original upload/execute flow and the 2.6.x Test Matrix feature are unchanged.
  - **Dynamic rows & columns**: per-project custom fields (13 data types) stored
    in a portable JSONB column; 16 built-in system fields.
  - **Cell restrictions, hints & comments**: required/range/pattern/enum/unique,
    CAN std/ext ID and timeout ranges, per-cell comments.
  - **Batch search/replace with preview & undo**: 11 operations, whole-batch
    transaction, audited, one-click undo.
  - **Excel import/export**: downloadable template, import **preview + error
    report** then transactional commit (insert/upsert/update/replace modes),
    formula-injection–safe export.
  - **RBAC, audit, optimistic locking, backup/restore**: 5 roles enforced
    server-side; full audit trail with secret redaction; `version`-based
    conflict detection (HTTP 409); `scripts/lm_backup.py` / `lm_restore.py` for
    PostgreSQL **and** SQLite.
  - **API** `/api/v1` with a unified response envelope + double-submit CSRF and
    HttpOnly/SameSite sessions. Bootstrap admin seeded on first boot
    (`LM_ADMIN_USER` / `LM_ADMIN_PASSWORD`). 51 new unit tests. Full reference:
    `docs/LAN_TEST_MATRIX.md`.

### 2.6.1
- **Test Matrix: blank projects + import fix.** You can now **create an empty
  project first** (`POST /api/matrices`) and then **import an Excel workbook into
  it** (`POST /api/matrices/<key>/import`, replace or append), in addition to the
  one-step "import as a new project". Also fixed a parse failure on upload
  (`'SpooledTemporaryFile' object has no attribute 'seekable'`) by buffering
  non-seekable upload streams before opening the workbook.

### 2.6.0
- **Test Matrix (Excel import / export).** New *additive* feature: import an
  Excel test-requirement workbook (e.g. `VHILS1_RWS_MCU_Qualification_Test_SYS.xlsx`)
  into a database-backed **test summary table** + **test items** (with procedure
  steps and input/expected signal headers), and export the **identical Excel
  layout** back out (round-trip lossless). New models `TestMatrix` / `TestItem`
  (tables `test_matrices`, `test_matrix_items`, auto-created on startup), a
  Flask-independent Excel codec (`app/services/matrix_excel.py`), the
  `/api/matrices` REST blueprint, `/matrices` pages, and a **Test Matrix** nav
  link. The existing upload/execute flow is unchanged. See `docs/TEST_MATRIX.md`.

### 2.5.0
- **Pre-warmed Silver instance pool.** The worker now launches `LICENSE_LIMIT`
  Silver instances at start-up and reuses them for every test instead of
  spawning a Silver process per run — faster job start-up and immediate license
  pre-emption. New config: `SILVER_POOL_ENABLED`, `SILVER_POOL_PREWARM`,
  `SILVER_POOL_RECONCILE_SECONDS`, `POOL_DIR`. The pool auto-resizes to the
  live license limit; the admin `in-use` figure now counts *busy* pooled
  instances. Set `SILVER_POOL_ENABLED=0` to keep the previous behaviour.
