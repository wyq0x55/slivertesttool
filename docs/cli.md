# Silver CLI：给 AI 与脚本使用的旧操作入口

在仓库根目录执行 `python -m silver_cli`。CLI 本身只依赖 Python 3.10+
标准库，不导入 Flask、不连接数据库、不启动 worker。它向已经运行的 Web
服务发送 HTTP 请求，复用浏览器使用的权限、校验、版本冲突及执行链路。

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

`items.patch_item` 仍需正文 `version` + `changes`。版本冲突返回 409，CLI 不替你
读取最新版本后强行覆盖。协同编辑活跃时原有 `COLLAB_ACTIVE` 写保护仍生效，
CLI 不绕过 CRDT 的单写者约束。用户角色也不会因为使用 CLI 而提升。

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

校验入口：`python -m pytest tests/test_cli_catalog.py tests/test_cli_runner.py
tests/test_cli_transport.py tests/test_cli_integration.py`。最后一项依赖专用可清空测试库；
不得指向生产库。实际结果和已知限制见 [验证记录](verification/cli-coverage-2026-10-03.md)。
