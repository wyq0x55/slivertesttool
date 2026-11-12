# Silver CLI：给 AI 与脚本使用的旧操作入口

在仓库根目录执行 `python -m silver_cli`。CLI 本身只依赖 Python 3.10+
标准库，不导入 Flask、不连接数据库、不启动 worker。它向已经运行的 Web
服务发送 HTTP 请求，复用浏览器使用的权限、校验、版本冲突及执行链路。

### 在其他电脑上使用（服务器部署后）

平台提供与自身 API 完全对应的单文件 CLI，无需平台源码或额外依赖：

```powershell
curl.exe -o silver-cli.pyz https://silver.example.internal/api/v1/cli/silver-cli.pyz
$env:SILVER_CLI_URL = 'https://silver.example.internal'
$env:SILVER_CLI_USERNAME = 'your-account'
python silver-cli.pyz --help
```

每个用户使用自己的账号，权限与浏览器完全相同。服务器升级后重新下载即可
保持命令目录一致。命令目录由 `scripts/export_cli_catalog.py` 从路由源码生成
到 `silver_cli/catalog.json`；修改路由后重新生成，测试会检查是否过期。

## 1. 发现命令和连接目标

```powershell
python -m silver_cli list
python -m silver_cli list --group tasks
python -m silver_cli describe tasks.run_selected_tasks
python -m silver_cli describe models.upload_project_model
```

`list` / `describe` 完全离线，输出 JSON。目录从现有路由源码推导；测试将目录
的每个 method/path 与 Flask 实际注册的 URL map 对照，防止出现第二份手工
维护的操作清单。命令名采用 `路由模块.处理函数`，例如 `projects.list_projects`。
**使用与服务器同版本的仓库**，升级后重新发现目录。

`path_params` 是必须显式提供的路径参数；`query_params`、`body_fields`、
`form_fields`、`file_fields` 是源码中发现的字段提示，**不是完整业务 JSON Schema**。
动态、嵌套、服务层校验仍以服务器为准。`response_kind` 区分 JSON、下载和 SSE。

连接前明确选择服务器，不默认猜测 localhost，不读取 `.env` 或数据库 DSN：

```powershell
$env:SILVER_CLI_URL = 'https://silver.example.internal'
$env:SILVER_CLI_USERNAME = 'your-account'
```

由调用器或密钥管理器注入 `SILVER_CLI_PASSWORD`，不要把真实密码写进命令参数、
文档或代码。提供用户名、密码时，每次调用先登录，在本进程内保存 cookie 和
CSRF，再执行操作。退出后不持久化凭据；下一次命令重新登录。可以不提供凭据
调用公共 `auth.health`，其他操作仍由服务器拒绝匿名请求。

`--url` 可以覆盖环境变量。HTTPS 使用系统/标准库的证书验证，不提供跳过验证
开关。仅 loopback HTTP 默认允许；明确接受可信内网明文传输风险时才使用
`--allow-http`。禁止 URL 内嵌凭据、重定向和自动读取代理环境变量。

### 登录一次，之后自动使用（推荐）

```powershell
python -m silver_cli login --url https://silver.example.internal --username your-account   # 隐藏输入密码
python -m silver_cli case list BRK          # 之后无需密码或环境变量
python -m silver_cli logout                 # 吊销并删除本机保存的 Token
```

`login` 用账号密码换取一个个人 API Token（同名旧 Token 自动吊销），保存在
`%APPDATA%\silver-cli\credentials.json`（Linux/macOS：`~/.config/silver-cli/`，
可用 `SILVER_CLI_CONFIG` 指定），文件只含 Token、不含密码，仅本人可读。未设置
`SILVER_CLI_URL` 时使用最近一次登录的服务器。凭据优先级：`SILVER_CLI_TOKEN` >
`SILVER_CLI_USERNAME`/`PASSWORD` > 已保存的登录。非交互环境可用 `--password-stdin`
或 `SILVER_CLI_PASSWORD`。

### 个人 API Token（推荐给 Agent 和脚本）

用账号密码登录一次签发 Token，之后只设置 `SILVER_CLI_TOKEN`，不再需要密码，
也不会因为频繁登录触发账号锁定：

```powershell
python -m silver_cli token create --name agent --days 90 --output agent.token --confirm
$env:SILVER_CLI_TOKEN = (Get-Content agent.token); Remove-Item Env:SILVER_CLI_PASSWORD
python -m silver_cli token list
python -m silver_cli token revoke 3 --confirm
```

- Token 以 `Authorization: Bearer` 发送，权限与所属账号完全相同，账号停用即失效。
- 密钥只在签发时写入指定的新文件一次；服务器只保存其 SHA-256。
- 有效期 1-365 天，每人最多 20 个有效 Token。Token 不能再签发 Token，不能管理账号（创建、修改、删除用户），也不能访问数据库控制台；这些操作返回 403 `PERMISSION_DENIED`（退出码 4），请用账号密码登录后执行。
- 管理员重置某用户的密码时，该用户的所有有效 Token 会被吊销，需要重新 `login` 或 `token create`。
- `SILVER_CLI_TOKEN` 与用户名/密码不能同时设置（`token create` 除外）。
- 网页端：侧栏底部「API Token」页面可签发、查看、吊销。
- 密钥事后无法再查看（服务器只存哈希）。忘记时用账号密码重新签发即可：
  `token create --name agent --days 90 --output agent.token --replace --confirm`
  会签发新 Token，并吊销同名的旧 Token。

## 2. 调用、预演与权限

```powershell
python -m silver_cli call projects.list_projects
python -m silver_cli call projects.get_project --param project_id=7
python -m silver_cli call items.list_items --param project_id=7 --query sheet=test --query page=1 --query page_size=100
python -m silver_cli call projects.create_project --json '@project.json' --dry-run
python -m silver_cli call projects.create_project --json '@project.json' --confirm
```

`project.json` 示例：`{"code":"DEMO","name":"CLI demo"}`。

- `--param NAME=VALUE`：每个路径参数恰好一次；拒绝重复、遗漏、额外参数和路径穿越。
- `--query NAME=VALUE`：保留顺序及重复参数。分页不会被自动展开；调用器读取服务器
  的分页/截断标记后继续翻页，避免悄悄少读数据。
- `--json '@file.json'` / `--json '@-'`：UTF-8 文件或标准输入，支持 BOM；仅接受对象，
  拒绝重复键及非有限数值。嵌套数组放在对象属性中。
- `--dry-run`：检查本地请求并展示方法、路径及字段名，**零网络请求，包括不登录**；
  不发送正文，也不验证远端业务规则。上传仍检查文件存在，下载仍要求新目标文件。
- 非 GET 操作一律要求 `--confirm`，包括导出 POST。它是防误操作开关，不是权限
  授予、审批证明或风险豁免。AI 审核、删除、清空、管理员 SQL 等动作应由调用器
  另行实施明确的人类授权策略，不能把 `--confirm` 当成自动批准。

Low-level `call items.patch_item` still requires `version` + `changes`. A stale
version returns 409; the CLI never forces an overwrite. Accepted field PATCHes
(including case, steps and shared-row edits) commit a durable mirror message
with the database update. They work while collaboration is active. The collab
server applies only the supplied cells, persists the Y.Doc and a delivery
receipt, then acknowledges the message. Stale materialization cannot overwrite
pending fields. An accepted PATCH takes effect when delivered; subsequent
browser edits remain valid, and retrying an already delivered message does not
replay it over those edits.

The version check compares against the database row, which trails the live
editor by its materialization delay (about 3 seconds). A PATCH can therefore
replace a cell a collaborator changed moments earlier without a
`VERSION_CONFLICT`; `steps` is replaced as one field.

While no room is active, every write (field, structural or bulk) goes to the
database only. The next time the project is opened in the collaborative editor,
the server folds those database changes into the persisted Y.Doc before anyone
connects: changed cells, new rows, deleted rows and row order. Nothing written
while the room was closed is reverted by the editor.

Structural and bulk operations retain `COLLAB_ACTIVE`: create/delete/move rows,
Excel import commit, IO extraction and recycle-bin row restore still require
the collaborative editor's reconciliation path or an inactive room. CLI access
does not change roles or permissions. The same outbox covers `test`, `const`,
`lib` and `io`.

## Typical flow: find a project, pick a case, fix its steps, rerun

```sh
python -m silver_cli project list --search brake          # 1. all projects -> code (BRK) or id
python -m silver_cli case list BRK                       # 2. every test id with its latest result
python -m silver_cli case list BRK --failed              #    or only the failing ones
python -m silver_cli case show BRK TC-1                  # 3. purpose and other fields, steps, latest verdict
python -m silver_cli case show BRK TC-1 --context        #    plus every const, io and lib row of the project
python -m silver_cli steps edit BRK --test-id TC-1 --step 2 --set timing=20 --confirm
python -m silver_cli steps add BRK --test-id TC-1 --json @step.json --at 3 --confirm
python -m silver_cli task run BRK --test-id TC-1 --wait --confirm   # 4. rerun and wait for the verdict
python -m silver_cli task run BRK --failed --wait --confirm         #    or every failing case at once
```

Edit cases and shared rows by business keys:

```sh
python -m silver_cli case set BRK TC-1 --set purpose="..." environment=SILVER --confirm
python -m silver_cli case add BRK --json @case.json --after TC-1 --confirm
python -m silver_cli case copy BRK TC-1 --as TC-9 --confirm
python -m silver_cli case delete BRK TC-9 --confirm          # recycle bin, restorable
python -m silver_cli row set BRK --sheet const --key SPEED_MAX --set const_value=130 --confirm
```

Every `PID` argument (and `task search --project`) accepts the numeric project
ID or the project code. A code is resolved once per invocation through
`project list`: an exact match wins, otherwise a unique case-insensitive match;
no match returns `PROJECT_NOT_FOUND`, several return `AMBIGUOUS_PROJECT` (exit 2).
Dry runs never resolve codes and show `<id of CODE>` in planned paths.

## High-level commands for agents

Use these task-oriented commands for common workflows. They use the same
transport, login, CSRF protection, permissions, and JSON envelope as `call`.
Discover arguments and examples with `python -m silver_cli --help` and
`python -m silver_cli steps set --help`. IDs below are examples; task IDs are
task keys such as `T000001`, rather than database integer IDs.

```sh
python -m silver_cli project list
python -m silver_cli project show 7
python -m silver_cli project create --json @project.json --dry-run
python -m silver_cli project create --json @project.json --confirm
python -m silver_cli item list 7 --sheet test --page 1 --page-size 100
python -m silver_cli item show 7 42
python -m silver_cli steps show 7 42
python -m silver_cli steps set 7 42 --json @steps.json --dry-run
python -m silver_cli steps set 7 42 --json @steps.json --confirm
python -m silver_cli steps edit 7 42 --step 1 --set description=Updated --dry-run
python -m silver_cli task list 7 --status failed
python -m silver_cli task show 7 T000001
python -m silver_cli task failures 7
python -m silver_cli task failures 7 --task T000001
python -m silver_cli task log 7 T000001 --tail 50
python -m silver_cli task rerun 7 T000001 --dry-run
python -m silver_cli task rerun 7 T000001 --confirm
```

| Command | Behavior and API operations |
|---|---|
| `project list` | `projects.list_projects` |
| `project show PID` | Aggregate `projects.get_project`, `fields.list_fields`, `models.list_project_models`, and `dashboard.project_dashboard_data` |
| `project create --json @FILE` | `projects.create_project` |
| `item list PID` | `items.list_items`; optional sheet and explicit pagination |
| `item show PID IID` | Find the item through `items.list_items`; there is no single-item GET endpoint |
| `steps show PID IID` | Extract the item's steps document |
| `steps set PID IID --json @FILE` | Read current item version, then `items.patch_item` with `version` and `changes.steps` |
| `steps edit PID IID --step N --set KEY=VALUE` | Read the document, edit one step (1-based index), and patch with the current version; repeat `--set` or give multiple pairs |
| `steps add PID IID --json @STEP [--at N]` | Insert one step object (default: append) and patch with the current version |
| `steps remove PID IID --step N` / `steps move PID IID --step N --to M` | Delete or reorder one step; signals and other steps are preserved |
| `--test-id TC-1` in place of `IID` | `item show` and every `steps` command can address a test-sheet row by its test id (`test_id`, else `case_id`); several rows with the same id return `AMBIGUOUS_TEST_ID` |
| `task list PID --status STATUS` | Read `tasks.list_project_tasks` and filter locally; the endpoint has no status query filter |
| `task show PID TID` | `tasks.project_task_detail`, including review data and events |
| `task failures PID [--task TID]` | Failed/error cases with judge result (`tasks.project_task_jdgrslt`) and error information |
| `task log PID TID --tail N` | Runtime events and errors from `tasks.project_task_detail`; limit displayed log lines within the first 5000 events |
| `task rerun PID TID` | `tasks.rerun_selected_tasks` with `{"task_keys":["T000001"]}` |
| `case list PID [--failed] [--status S\|not_run]` | Test-sheet items joined with the latest run: test id, item id, title, status, result, task key (two requests) |
| `case show PID TEST_ID [--context]` | One case: every filled test-row field with its display name (目的 etc.), steps, version, latest run with verdict, jdgrslt and errors; `--context` adds all const, io and lib rows |
| `project context PID [--sheet const\|io\|lib]` | Every row of the project's const, io and lib sheets with field display names |
| `case set PID TEST_ID --set FIELD=VALUE` | Patch case fields (not steps) with the current item version |
| `case add PID --json @FILE [--after TEST_ID]` | `items.create_item` on the test sheet; refuses an existing test_id |
| `case copy PID TEST_ID --as NEW_ID` | Create a copy below the source without run results (result, exec_date, executor, log) |
| `case delete PID TEST_ID` | `items.delete_item` (soft delete to the recycle bin) |
| `row set PID --sheet const\|io\|lib --key NAME --set FIELD=VALUE` | Find the row by const_name, io_name or lib_func (must be unique) and patch it |
| `task run PID --failed\|--not-run\|--all` | Select cases as `case list` does, then submit them in one request |
| Run outcomes (`task wait`, `task report`, `case show`) | Include `step_results` (per step: result, time, description, checks with target/expected/observed/timing) and `failed_checks` when the server provides structured jdgrslt |
| `project list --search TEXT` | `projects.list_projects`, filtered locally by code, name and description |
| `task search [--project PID] [--q TEXT] [--status S] [--mine]` | `me.me_tasks`: runs across every visible project |
| `task run PID --test-id ID... [--model NAME@VERSION] [--wait]` | `tasks.run_selected_tasks`; `--wait` then behaves like `task wait` |
| `task wait PID TID...` | Poll `tasks.project_task_status`, then report detail and jdgrslt per run; exit 8 if any run did not pass, 9 on timeout |
| `task report PID [--test-id ...] [--status S\|not_run] [--output FILE]` | Test-sheet items joined with each case's latest run, verdict, jdgrslt and errors; `.md` output writes Markdown |
| `task cancel PID TID` / `task download PID TID --output FILE` | `tasks.cancel_project_task` / `tasks.download_project_task` |
| `model list/upload/use PID` | `models.list_project_models` / `upload_project_model` (multipart dll, sbs, pdb, extra) / `set_current_project_model` |
| `ut options/config PID` | `ut_pipeline.manifest_options` / `get_config` |
| `ut configure PID --json @FILE` | `ut_pipeline.put_config`; fields are listed in `--help` |
| `ut validate-manifest PID --json @FILE` / `ut paths PID --path DIR` | `validate_manifest` (persists nothing, no `--confirm`) / `browse_server_paths` |
| `ut import-targets PID` / `ut targets PID [--function F]` | `import_targets` / `list_targets` |
| `ut run PID --function F... [--source S] [--wait]` | Resolve each function to exactly one indexed target, then `create_batch`; `--wait` polls `list_jobs` |
| `ut wait PID --batch B\|--job J...` | Poll until no job is queued, generating, executing or repairing |
| `ut jobs/batches/job/stats PID` | `list_jobs` / `list_batches` / `job_detail` / `repair_stats` |
| `ut evidence PID JID [--attempt A]` | `job_detail` plus `attempt_csv?format=text`, `format=diagnostic` and `attempt_coverage`; a missing section is reported, not fatal |
| `ut revise PID JID --csv FILE [--note]` | `submit_revision` with the file uploaded byte for byte |
| `ut retry/stop PID JID`, `ut stop-batch PID B` | `retry` / `stop_job` / `stop_batch` |
| `ut review PID JID --decision approve\|reject [--note]` | Read `current_attempt_id`, then `review` with `expected_attempt_id`; never approve without a person's consent |
| `ut export PID [--batch B] --output FILE` | `export_approved` zip |

The input to `steps set` is the steps document itself, for example:

```json
{"input_signals":[],"expected_signals":[],"steps":[]}
```

For `steps edit`, values parse as JSON when valid, otherwise as text; keys are
literal field names rather than dotted paths. For example, `--set enabled=true`
sets a boolean. Each field may be assigned only once in a command.

Inspect the actual document with `steps show` before editing it. Preserve signal
definitions, step fields, and ordering required by that project. The high-level
commands do not replace server validation or real Silver execution.

Every write supports mutually exclusive `--dry-run` and `--confirm` flags and
requires `--confirm` before any network request, including the preliminary
item/version read. Dry runs validate local
input and describe the planned workflow without logging in or reading remote
state; they cannot validate the remote version or step index against live data.
For a confirmed steps write, the CLI reads the current version and submits one
PATCH. A stale version returns `VERSION_CONFLICT` (exit 5); inspect the latest
document, reconcile the change, and retry only within the authorized scope. The
CLI does not silently rebase or retry the write. Project locks remain enforced;
`COLLAB_ACTIVE` still protects structural and bulk writes. Versioned field
PATCHes use the durable collaboration delivery path described above.

Task listing reads the newest 2000 runs and can be truncated by the server's
growing-window limit; inspect `total`, `truncated`, and returned counts before
treating a failure report as exhaustive.
Failure reports also include runs with a failing/error judge verdict, even when
their execution status is not failed. Log output includes log, warning, and
error events, with task error details preserved separately from the tailed lines.
The detail endpoint returns at most the first 5000 events; `--tail` selects lines
within that window, and `possibly_truncated` warns when the window is full.
`task rerun` reuses the original model and regenerates runner input from current
project rows. Inspect `created`, `skipped`, `missing`, and `errors`, then query
status and judge results. A successful HTTP response does not prove a test PASS.

## 3. 文件、模型和异步任务

### Excel 预览、提交、导出

```powershell
python -m silver_cli call imports_exports.create_import --param project_id=7 --file 'file=C:\inputs\matrix.xlsx' --form mode=upsert --confirm
python -m silver_cli call imports_exports.get_import --param job_id=12
python -m silver_cli call imports_exports.commit_import --param job_id=12 --confirm
python -m silver_cli call imports_exports.export_test_matrix --param project_id=7 --output 'C:\outputs\matrix.xlsx'
```

先审查预览结果，再提交。`replace_all` 仍受原有更高权限限制。Const、Lib(Func)、
I/O 等专用格式分别使用 `imports_exports.import_const/import_libfunc/import_io`。
不要把生成的 CLI 返回值误当成已经完成所有行的业务成功；检查服务器的 errors、
missing、duplicates、skipped 等细项。

上传使用有 Content-Length 的分块 multipart，文件不整体读进内存。`--form`
和 `--file` 均可重复，顺序不丢失；文件正文不能与 `--json` 混用。例如旧文件夹
提交协议用同顺序的 `files` 和 `paths` 表达目录结构：

```powershell
python -m silver_cli call tasks.upload_project_tree --param project_id=7 --form model=plant@v1 --form test_ids=TC-1 --form paths=TC-1/judge.py --file 'files=C:\cases\TC-1\judge.py' --confirm
```

实际需要哪些用例文件，由服务器既有校验决定；其余文件按顺序追加同名参数。
`lib_files/lib_paths` 与 `stdlib_files/stdlib_paths` 同理。CLI 不自动递归扫描目录，
避免把未明确选择的代码、凭据或大文件上传。

### 模型与运行

```powershell
python -m silver_cli call models.list_project_models --param project_id=7
python -m silver_cli call models.upload_project_model --param project_id=7 --form name=plant --form version=v1 --file 'dll=C:\model\plant.dll' --file 'sbs=C:\model\plant.sbs' --file 'pdb=C:\model\plant.pdb' --confirm
python -m silver_cli call tasks.run_selected_tasks --param project_id=7 --json '@run.json' --confirm
```

`run.json`：`{"model":"plant@v1","test_ids":["TC-1","TC-2"]}`。
注册服务器可见的 `.sil` 用 `models.add_project_model`，正文包含 `name`、`version`、
`path`；这里的 path 是**服务器路径**，不是 CLI 机器的本地文件。服务器继续保存
项目本地模型副本，并为运行固定输入。CLI 不引入第二个 worker，也不改变并发上限。
真实 DLL/SBS/PDB 注册和任务提交可能消耗 Silver 资源；不要在生产随手运行示例。

```powershell
python -m silver_cli wait tasks.project_task_status --param project_id=7 --param task_key=T000001 --status-path data.task.status --success passed --failure failed --failure cancelled --pending queued --pending running --max-wait 300
python -m silver_cli call tasks.project_task_jdgrslt --param project_id=7 --param task_key=T000001
python -m silver_cli call tasks.download_project_task --param project_id=7 --param task_key=T000001 --output 'C:\outputs\T000001.zip'
```

`wait` 仅允许 GET JSON 操作。成功/失败/等待状态必须显式提供且互斥；未知状态
或缺失状态字段立即报错。`--interval` 默认 1 秒，`--max-wait` 默认 300 秒，
`--timeout` 为每个请求的 socket timeout，默认 30 秒；轮询请求使用剩余预算。
等待超时或 Ctrl+C **不取消远端任务**。执行 status 与 judge result 不同：即使
等待的 status 满足条件，也要检查 `result`、日志和归档，不能据此伪造验收 PASS。

重试运行使用 `tasks.rerun_selected_tasks`，正文 `{"task_keys":["T000001"]}`；
取消使用 `tasks.cancel_project_task`。二者都要求 `--confirm`。客户端不自动重试
任何写请求，网络中断后应先查询服务器，防止重复创建或审批。

下载必须指定一个**不存在**的文件，父目录须已存在。忽略服务器建议文件名，
流式落盘并返回 bytes/SHA-256，不覆盖旧文件；失败清理此次创建的半成品。
已有归档的服务端路径边界检查保持不变。

### AI 草稿与事件

```powershell
python -m silver_cli call ai.list_drafts --query project_id=7
python -m silver_cli call ai.create_draft --json '@draft-request.json' --confirm
python -m silver_cli wait ai.get_draft --param draft_id=18 --status-path data.status --success pending --failure error --failure cancelled --pending running
python -m silver_cli call tasks.project_task_stream --param project_id=7 --param task_key=T000001 --query last_id=0 --max-events 1000
```

待审 `pending` 表示生成结束，不代表审核通过。提交、编辑、审批、驳回、取消、
重试均是不同操作；CLI 不自动串联审批。配额/收费仍由原 AI provider 管理。

SSE 输出一行一个 JSON envelope，data 含 `event/id/data`。达到 `--max-events`
或服务器 `stream-timeout` 返回退出码 9；保留最后 id 后显式传 `--query last_id=...`
恢复。断流但没收到 end 返回 6，不装成正常结束。流结束本身不是测试通过证明。

## 4. 覆盖矩阵与边界

当前基线主线 `52d0882`：**138 个 method/path，15 个操作组**，其中 127 个 JSON、
10 个文件下载、1 个 SSE。后续目录随源码变化，实际数量用 `list` 查询。

| 组 | 数量 | 旧操作范围 |
|---|---:|---|
| projects / members / fields | 6 / 5 / 4 | 项目、成员角色、动态字段、协同 token 请求 |
| items / reviews | 16 / 8 | 测试行、参考池、复制恢复、批量更新、评论、审核与豁免 |
| models / tasks | 12 / 14 | 保存的模型、SBS 版本、提交重跑取消、日志、报告 |
| imports_exports / audit_trash / dashboard | 14 / 6 / 5 | Excel、审计、回收站、仪表盘、历史与版本比较 |
| auth / me / ai / admin_console / admin_db | 6 / 8 / 14 / 12 / 8 | 会话、个人工作台、通知、AI 草稿与配置、系统管理和数据库管理 |

范围是**已有 HTTP 业务操作的命令行可达性**，不是 138 个业务场景都跑过真实
Silver/真实 AI 的声明。命令不会替代浏览器排版、协同光标、实时 Y.Doc 客户端；
协同 token 在输出中脱敏，此命令不用于建立 WebSocket 编辑会话。

部署管理保留既有入口，不通过 HTTP 重做一份：`python manage.py bootstrap`、
`python run_web.py`、`python run_worker.py`、`python run_collab.py`、`python run.py`。
效率试点仍使用 `python -m scripts.efficiency_pilot`，见 [效率试点指南](efficiency-pilot.md)。
这些本地管理入口有自己的运行/数据库边界，不受 `silver_cli --dry-run` 保护。

## 5. 机器契约与退出码

除显式 `--help` 外，stdout 只输出 JSON；SSE 是 JSON Lines。中文使用 JSON 转义，
Windows 旧终端也可无损解析。保留业务 data 和 request_id；密码、密钥、cookie、
会话 token 脱敏，token 使用计数保留。错误增加 HTTP status（若存在）。

| 退出码 | 含义 |
|---:|---|
| 0 | 请求成功；wait 满足指定成功状态；不是整批业务或测试 PASS 的保证 |
| 2 | 本地参数/确认/文件错误，或其他 HTTP 4xx |
| 3 / 4 / 5 | 认证或无效登录响应 / 权限 / 版本冲突或账号锁定 |
| 6 / 7 | 网络/重定向/断流 / 服务端错误或响应协议错误 |
| 8 / 9 / 130 | wait 指定的失败终态 / 等待或流额度耗尽 / 用户中断 |

`wait` 失败终态保留服务器成功查询的 envelope，因此其 `success` 可能仍为 true，
**同时检查进程退出码与业务内容**。请求失败后的远端写入状态可能未知，不自动重试。

校验入口：`python -m pytest tests/test_cli_catalog.py tests/test_cli_runner.py tests/test_cli_commands.py
tests/test_cli_transport.py tests/test_cli_integration.py`。最后一项依赖专用可清空测试库；
不得指向生产库。实际结果和已知限制见 [验证记录](verification/cli-coverage-2026-10-03.md)。
