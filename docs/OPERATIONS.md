# 运维指南

本文档适用于本地和预生产环境。本项目目前尚未获准用于生产环境。

## 进程隔离

请将 API、Worker 和 MCP Server 作为独立进程运行。只有 Worker 可以获得数据库 URL 环境变量。

| 变量 | API | Worker | MCP |
|---|---:|---:|---:|
| \`SAFEDB_AGENT_TOKEN\` | 是 | 否 | 是 |
| \`SAFEDB_REVIEWER_TOKEN\` | 是 | 否 | 否 |
| \`SAFEDB_DB_PATH\` | 是 | 是 | 否 |
| \`SAFEDB_DATASOURCES_FILE\` | 是 | 是 | 否 |
| \`SAFEDB_API_URL\` | 否 | 否 | 是 |
| \`MYSQL8_URL\` / \`PG16_URL\` / \`ORACLE19C_URL\` | **否** | 是，仅设置所需 URL | **否** |

API 和 Worker 必须指向同一个提案存储和数据源配置。在当前原型中，这意味着二者使用同一个本地 SQLite 文件路径；使用容器时，需要挂载共享卷。

## Worker 设置

- \`SAFEDB_WORKER_INTERVAL_SECONDS\`：轮询间隔，默认值为 \`1\` 秒，必须为正数。
- \`SAFEDB_WORKER_LEASE_SECONDS\`：过期任务认领恢复租约，默认值为 \`600\` 秒，必须为正数。

租约时间应明显长于最长的分析或执行操作，包括连接获取、查询规划、查询执行和结果获取。租约过短会导致第二个 Worker 把实际上只是执行较慢的任务重新认领。Fencing token 可以阻止旧 Worker 发布过期状态/结果，但无法阻止已经开始的数据库工作重复执行。

## 提案状态与恢复

存储层按以下方式重新排队无人处理的任务：

| 被放弃时的状态 | 恢复后的状态 |
|---|---|
| \`ANALYZING_WORKER\` | \`ANALYZING\` |
| \`EXECUTING\` | \`APPROVED\` |

租约恢复会清除旧 fencing token，并以原子方式写入审计事件。使用旧 token 恢复运行的 Worker 无法写入完成状态或发布结果。当前 SQLite 存储仅适合单机开发，不要将其扩展到多主机部署。

## 健康检查与接口

- \`GET /health\` 仅用于检查 API 进程是否存活，不是数据库就绪探针。
- \`GET /v1/datasources\` 只列出当前已认证 Agent 获准访问的数据源。
- \`GET /v1/review/queue\` 需要 Reviewer Token。
- \`GET /v1/queries/{id}/result\` 仅允许提交该提案且执行成功的 Agent 访问。

当前健康检查接口不会验证 Worker 是否存活、数据库是否可达、队列等待时间或查询是否成功执行。生产环境前应增加专门的就绪检查、Worker 心跳、队列深度、错误率和数据库连通性监控。

## 密钥管理

- Agent Token 和 Reviewer Token 必须相互独立，并具有足够高的熵。
- 使用最小权限数据库账号；每个数据源应使用独立的密钥身份。
- 不要将数据库 URL 放入 API/MCP 环境变量或 MCP 客户端配置。
- 优先通过 Secret Manager/Vault 向 Worker 注入密钥，而不是使用普通环境文件。
- 将 Worker 网络出口限制为已配置的数据库主机和端口。
- 如果主机、进程、环境文件、日志或密钥身份可能暴露，应立即轮换数据源凭证以及 Agent/Reviewer Token。

## 运维限制

- SQLite 不是生产级队列，也不适用于多主机协调。
- 静态 Bearer Token 仅用于本地开发身份验证，不是企业级身份体系。
- 当前 API 持久化 SQL、参数和结果时，没有应用层静态加密。
- 当前没有内置速率限制、按主体配额、队列深度限制、指标流水线或不可篡改的外部审计存储。
- 数据库驱动可能在响应字节数限制生效前，先将单个超大字段/LOB 完整加载到内存。
- 单元测试不能替代 MySQL 8、PostgreSQL 16 和 Oracle 19c 的真实数据库集成测试。
