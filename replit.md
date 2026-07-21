# [Project name]

_Replace the heading above with the project's name, and this line with one sentence describing what this app does for users._

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

_Populate as you build — short repo map plus pointers to the source-of-truth file for DB schema, API contracts, theme files, etc._

## Architecture decisions

_Populate as you build — non-obvious choices a reader couldn't infer from the code (3-5 bullets)._

## Product

_Describe the high-level user-facing capabilities of this app once they exist._

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package P0 — 进程启动 / 循环依赖（其他所有优化的前提）

问题： run_worker.py 里 Flask 被初始化了两次；api_routes.py 有注释说明 routes → tasks → create_app → routes 的循环依赖靠懒加载破解。

目标文件： run_worker.py、app/jobqueue/tasks.py、app/__init__.py

修复目标： 每个进程只构造一次 app，消除模块级副作用启动。

🟠 P1a — 路由 God Module（1458 行）

问题： app/routes/lanmatrix_api.py 一个文件混了认证、CRUD、Excel 导入、任务、管理 DB、管理控制台六种职责；api_routes.py 用另一套鉴权模型重复实现了任务/管理逻辑。

修复目标： 按业务边界拆分为 auth、projects_items、tasks、admin_db、admin_console 五个 Blueprint。

🟠 P1b — 前后端协议重复定义

问题： Sheet key、步骤字段映射、房间命名在 4 个地方各自硬编码：

app/services/lanmatrix/fields.py
app/collab/doc_model.py
app/static/js/lanmatrix/editor.js
app/static/js/lanmatrix/collab.js
修复目标： 后端出一份 /api/config 或静态 JSON，前端消费它，消除并行 schema 定义。

🟡 P2 — 协作房间内存泄漏

问题： app/collab/server.py 中 _rooms/_materializers 无限累积（auto_clean_rooms=False），长时间运行内存会持续增长。

修复目标： 加空闲驱逐，关闭时调用 Materializer.detach()。

🔐 安全问题

问题： app/routes/api_routes.py 里 /api/admin/* 仍用 ADMIN_TOKEN（config.py 有默认值），而新管理功能用的是 session RBAC。默认 token 允许任何已登录用户提权。

修复目标： 统一用 system_admin session 鉴权，废弃 token 鉴权路径。


