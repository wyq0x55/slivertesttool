# AI Agent 接入设计（测试资产生成 · 人工只审核）

> 分支：`feature/ai-agent`。设计讨论定稿见私有仓库 `unit-test-automation` 的
> 《SILS测试自动化方案》。本文档描述其在 slivertesttool 中的落地实现。

## 1. 设计原则

1. **AI 只产草稿，不直接写库**。所有输出进入 `lm_ai_drafts` 表：提交后
   `running`（worker 生成中）→ `pending`（待审），由人工 `approve`（经既有
   服务层落库）或 `reject`（必须填原因）。批量手顺支持**部分通过**（勾选
   `refs`），审批前可**内联编辑**输出（`meta.edited` 留痕）。
2. **每类写入都有机器校验兜底**：生成走 generate → validate → retry 循环
   （最多 3 轮，校验问题回喂模型，批量场景只重试失败条目）；变量名等事实性
   内容由确定性代码保证。
3. **确定性优先**：变量/函数清单由 `c_index` 用 **libclang AST** 抽取
   （`pip install libclang`，wheel 自带原生库），`#ifdef` 按传入的
   `compile_args`（建议来自 compile_commands.json）求值、作用域由 AST 区分、
   多声明符完整收录；模型 prompt 只拿到真实存在的名字，编造会被校验器拒绝。
4. **语义注册表自动组装，零人工维护**（`registry.py`）：表示名 ↔ 路径的映射
   按优先级合并——clang 声明注释 < SBS 文本 < 观点种子 < 历史手顺表头 <
   **项目信号字典**（唯一的例外：`lm_ai_signal_dict` 人工维护，优先级最高，
   用于冷启动项目或供应商无注释代码）。
5. **省 token 的输出格式**（`sparse.py`）：手顺按名字键稀疏输出
   （`{"veh_speed": "120"}`），展开器确定性回填表示名与空位，比按位对齐
   数组省 40–60% 输出 token；两阶段流水线（规划 → 批量稀疏手顺）让同一
   模块的上下文在所有观点间共享（prompt 缓存友好）。
6. **lib 人工提议、AI 编写**：lib 只在人工标注"适合做成 lib"后由 AI 生成，
   入库动机是真实复用。

## 2. 新增模块

```
app/models/ai_draft.py             AiDraft 草稿表 + AiSignalDict 项目信号字典
app/services/ai/
    config.py        LLM 网关配置（app_settings 优先，环境变量兜底，密钥永回显掩码）
    provider.py      OpenAI 兼容 chat-completions 客户端（纯 stdlib urllib）+ JSON 抽取
                     + token 用量出参（usage.input/output_tokens）
    base.py          generate → validate → retry 循环；usage 全轮次累计
    c_index.py       C 源码索引（libclang AST：全局变量/函数，#ifdef/作用域/多声明符精确）
    registry.py      语义信号注册表（四自动来源 + 信号字典，按优先级合并）
    sparse.py        稀疏输出 → 平台 steps_doc 的确定性展开（回填表示名/空位）
    prompts.py       五个场景的 prompt 构造（固定长前缀，prompt 缓存友好）
    validators.py    机器校验器（steps schema、名字存在性、SBS 括号配平等）
    scenarios.py     五个场景编排（纯函数，无 Flask/DB 依赖；支持 on_event 进度回调）
    signal_dict.py   项目信号字典的读写（bulk replace）
    apply.py         approve 落库：全部走 items_service / SbsRevision / CellComment
app/jobqueue/tasks.py             run_ai_generation：Huey 任务（worker 侧执行）
app/routes/lanmatrix/ai.py        /api/v1/ai/* REST 端点
app/templates/lanmatrix/ai_drafts.html + static/js/lanmatrix/ai_drafts.js
                                  审核页（生成/轮询/部分通过/内联编辑/用量/字典）
tests/test_ai_unit.py             61 个纯逻辑测试（无需数据库）
tests/test_ai_api.py              API 集成测试（需 PostgreSQL 测试库）
```

## 3. 五个场景

| 场景 | 输入 | 输出草稿 | approve 落库 |
|---|---|---|---|
| `viewpoint` | 设计书文本 | 观点数组（正例/反例/边界/组合，含模块 ID 追溯） | 每观点一行 Draft 测试行（Test-Matrix 字段映射） |
| `procedure` | 观点 + 源码文件 + SBS 变量 + lib 清单 | steps JSON（手顺）+ missing_variables | 写入目标行的 `steps` 字段 |
| `sbs` | 源码 + 既有 SBS | SBS 增量文本 + needed_variables | 追加一条 `SbsRevision` 快照（走既有版本 UI 启用） |
| `lib` | 人工提议 + 被标注手顺 | lib 函数（lib_stb/lib_para）+ 改写后手顺 | 建 lib sheet 行 + 改写引用手顺 |
| `failure` | 失败用例日志段落 + 观点 | 差异分析/原因/建议/分类 | 挂 `CellComment`（field_key=ai_failure_analysis） |

关键闭环：`procedure` 发现的 `missing_variables` → 触发 `sbs` 场景补登记 →
SBS revision 构筑验证（Silver 构筑在平台外部执行，见"边界"）。

`procedure` 是两阶段流水线：Phase A 规划（观点 → 变量/值映射，小输出、
强校验）→ Phase B 批量稀疏手顺（每 8 条一批，逐条定向重试，交叉校验
"plan 里的每个变量都必须出现在步骤中"——防"漂亮但没测到点上"）。
进度经 `on_event` 回调写入 `meta.progress`（"规划中" / "手顺批次 2/5
第 1 轮" / "完成"）。

## 4. REST API

```
GET  /api/v1/ai/settings              系统管理员：查看配置（密钥掩码）
PUT  /api/v1/ai/settings              系统管理员：更新 api_base/api_key/model/timeout
GET  /api/v1/ai/scenarios             场景目录
POST /api/v1/ai/drafts                {scenario, project_id, payload} → 草稿（异步）
GET  /api/v1/ai/drafts?project_id=&scenario=&status=   草稿列表（status 含 running）
GET  /api/v1/ai/drafts/<id>           草稿全文（审核对话框/轮询数据源）
PUT  /api/v1/ai/drafts/<id>           {output} 审批前内联编辑（pending/error，meta.edited）
POST /api/v1/ai/drafts/<id>/approve   审核通过并落库；可选 {refs:[...]} 部分通过
POST /api/v1/ai/drafts/<id>/reject    驳回（必须 note）
GET  /api/v1/ai/usage?project_id=&months=   按项目聚合 token 用量（场景/月份维度）
GET  /api/v1/ai/signals?project_id=   项目信号字典
PUT  /api/v1/ai/signals               {project_id, entries:[[表示名,路径,类型?],...]} 整体替换
```

权限：生成/审核/字典写入走项目 `item.edit`；查看/用量走 `project.view`；
配置走系统管理员。状态变更请求带 `X-CSRF-Token`（与其他 /api/v1 蓝图一致）。

**异步语义**：`POST /ai/drafts` 创建 `running` 草稿并入队 `run_ai_generation`
（Huey），立即返回；worker 执行场景并把进度写进 `meta.progress`，完成后置
`pending`（失败置 `error`，原因在 `error` 字段）。前端提交后轮询
`GET /ai/drafts/<id>` 直到离开 `running`。测试环境 Huey 为 immediate 模式
（内联执行），行为等价于同步。

**用量**：每次 LLM 调用的 token 用量（含重试轮次）全链路累计在
`meta.usage`（`input_tokens`/`output_tokens`），`GET /ai/usage` 按项目/
场景/月份聚合。

## 5. LLM 网关配置与部署要求

管理员在系统设置中填 `ai_api_base` / `ai_api_key` / `ai_model` /
`ai_timeout`（存 `app_settings`，web 与 worker 共享）；也可用环境变量
`SILVERTOOL_AI_*` 引导。任何 OpenAI 兼容端点（GLM / DeepSeek / DashScope
兼容模式 / vLLM / Ollama / 内网网关）均可。温度默认 0.2（测试资产要一致
性不要创造性）。

**部署要求**：生成在 Huey worker 上执行，`run_worker.py` 必须常驻运行；
否则草稿会停留在 `running`（目前没有僵尸 running 的自动回收）。

## 6. 实现边界（明确不在本次范围）

- **Silver 构筑/dry-run 验证**：`sbs` 草稿目前做文本级校验（括号/引号配
  平、变量存在于源码索引）；接 Silver 构筑命令行后，在 `apply_sbs` 前插入
  构筑校验钩子即可（`apply.py` 已按此结构组织）。
- **设计书解析**（docx/xlsx → 文本）：`viewpoint` 场景接收文本输入；
  文件解析器（python-docx 已随 openpyxl 思路预留）作为后续增量。
- **生成 payload 的表单化**：目前 payload 以 JSON 文本提交（各场景字段
  见第 3 节）；测试矩阵以 univer sheet 展示，表单化生成入口作为后续增量。

## 7. 测试

- 新增依赖：`libclang>=16,<19`（已加入 requirements.txt / pyproject.toml）。
- `tests/test_ai_unit.py`（无 DB，61 个）：JSON 抽取、校验器、C 索引（含
  块内局部不误收 / `int a, b;` 双收 / `#ifdef` 随编译参数变化等 AST 精度
  用例）、注册表（来源优先级/歧义/空表示名不覆盖）、稀疏展开、两阶段
  手顺（定向重试/失败留存/交叉校验）、usage 累计、进度事件、信号字典
  优先级。`pytest tests/test_ai_unit.py`
- `tests/test_ai_api.py`（需 PG，17 个）：配置掩码、异步草稿生命周期、
  procedure 落库、部分通过（勾选/全量）、内联编辑后落库、用量聚合、
  信号字典（写入/校验/注入生成端到端）。
  `LM_ALLOW_INSECURE_SECRET=1 pytest tests/test_ai_api.py`
