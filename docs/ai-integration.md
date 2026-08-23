# AI Agent 接入设计（测试资产生成 · 人工只审核）

> 分支：`feature/ai-agent`。设计讨论定稿见私有仓库 `unit-test-automation` 的
> 《SILS测试自动化方案》。本文档描述其在 slivertesttool 中的落地实现。

## 1. 设计原则

1. **AI 只产草稿，不直接写库**。所有输出进入 `lm_ai_drafts` 表，状态
   `pending`，由人工 `approve`（经既有服务层落库）或 `reject`（必须填原因）。
2. **每类写入都有机器校验兜底**：生成走 generate → validate → retry 循环
   （最多 3 轮，校验问题回喂模型）；变量名等事实性内容由确定性代码保证。
3. **确定性优先**：变量/函数清单由 `c_index` 从源码正则抽取（无 libclang
   依赖），模型 prompt 只拿到真实存在的名字，编造会被校验器拒绝。
4. **lib 人工提议、AI 编写**：lib 只在人工标注"适合做成 lib"后由 AI 生成，
   入库动机是真实复用。

## 2. 新增模块

```
app/models/ai_draft.py             AiDraft 草稿表（场景/状态/输入输出/校验日志）
app/services/ai/
    config.py        LLM 网关配置（app_settings 优先，环境变量兜底，密钥永回显掩码）
    provider.py      OpenAI 兼容 chat-completions 客户端（纯 stdlib urllib）+ JSON 抽取
    base.py          generate → validate → retry 循环
    c_index.py       C 源码索引（全局变量/函数/结构体，token 控制的上下文选择）
    prompts.py       五个场景的 prompt 构造（系统提示词统一，输出严格 JSON）
    validators.py    机器校验器（steps schema、名字存在性、SBS 括号配平等）
    scenarios.py     五个场景编排（纯函数，无 Flask/DB 依赖，可独立测试）
    apply.py         approve 落库：全部走 items_service / SbsRevision / CellComment
app/routes/lanmatrix/ai.py        /api/v1/ai/* REST 端点
tests/test_ai_unit.py             38 个纯逻辑测试（无需数据库）
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

## 4. REST API

```
GET  /api/v1/ai/settings              系统管理员：查看配置（密钥掩码）
PUT  /api/v1/ai/settings              系统管理员：更新 api_base/api_key/model/timeout
GET  /api/v1/ai/scenarios             场景目录
POST /api/v1/ai/drafts                {scenario, project_id, payload} → 草稿（同步，最多 3 轮）
GET  /api/v1/ai/drafts?project_id=&scenario=&status=   草稿列表（项目内，item.edit）
GET  /api/v1/ai/drafts/<id>           草稿全文（审核对话框数据源）
POST /api/v1/ai/drafts/<id>/approve   审核通过并落库（item.edit）
POST /api/v1/ai/drafts/<id>/reject    驳回（必须 note）
```

权限：生成/审核走项目 `item.edit`；查看走 `project.view`；配置走系统管理员。
状态变更请求带 `X-CSRF-Token`（与其他 /api/v1 蓝图一致）。

## 5. LLM 网关配置

管理员在系统设置中填 `ai_api_base` / `ai_api_key` / `ai_model` /
`ai_timeout`（存 `app_settings`，web 与 worker 共享）；也可用环境变量
`SILVERTOOL_AI_*` 引导。任何 OpenAI 兼容端点（GLM / DeepSeek / DashScope
兼容模式 / vLLM / Ollama / 内网网关）均可。温度默认 0.2（测试资产要一致
性不要创造性）。

## 6. 实现边界（明确不在本次范围）

- **Silver 构筑/dry-run 验证**：`sbs` 草稿目前做文本级校验（括号/引号配
  平、变量存在于源码索引）；接 Silver 构筑命令行后，在 `apply_sbs` 前插入
  构筑校验钩子即可（`apply.py` 已按此结构组织）。
- **设计书解析**（docx/xlsx → 文本）：`viewpoint` 场景接收文本输入；
  文件解析器（python-docx 已随 openpyxl 思路预留）作为后续增量。
- **前端审核界面**：先以 API 交付，矩阵编辑器中的"AI 草稿"面板是下一步。

## 7. 测试

- `tests/test_ai_unit.py`（无 DB）：JSON 抽取、校验器、C 索引、重试循环、
  五场景（脚本化假模型，含"编造变量被打回"用例）。`pytest tests/test_ai_unit.py`
- `tests/test_ai_api.py`（需 PG）：配置掩码、草稿生命周期、procedure 落库。
  `pytest tests/test_ai_api.py`
