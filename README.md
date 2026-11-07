# SafeDB-MCP

面向 AI Agent 的受控 SQL 查询网关。API 和 MCP Server 接收查询请求；独立的 Worker 持有数据库凭证，并负责执行获准的查询。

> **项目状态：安全原型 / 早期 MVP，尚未达到生产可用标准。** 当前 CI 使用单元测试和 Mock，尚不能证明数据库服务器层面的安全约束确实生效。请勿连接具有生产权限的数据库凭证。

## 功能概览

- 每次接收一个只读查询请求，并使用固定版本的 SQLGlot 解析器进行校验。
- 根据配置的数据源、Agent 授权关系和带 Schema 限定的表白名单限制查询范围。
- 拒绝非 SELECT 语句、SQL 注释载体、存在歧义的 MySQL 词法形式、不支持的标识符、不安全函数、通配符投影以及不支持的查询结构。
- 要求 MySQL 8、PostgreSQL 16 和 Oracle 19c 使用数据库原生只读事务。
- 在安全的情况下估算查询成本，应用行数、字节数和时间限制；风险不属于低风险的查询必须进入审核流程。
- API 和 MCP 进程不接收数据库 URL。只有 Worker 能获取数据源密钥。
- 使用 Worker fencing token（隔离令牌），防止租约过期的旧 Worker 覆盖新 Worker 的状态或发布过期结果。

## 架构

\`\`\`text
Agent
  |
  v
MCP Server -- HTTP + Agent Token --> API ----> Proposal Store
                                            ^          ^
                                            |          |
                                      Reviewer API    Worker
                                     独立审核令牌       |
                                       批准/拒绝       +--> DB Secret --> Database
                                                       +--> 重新校验/规划/执行
\`\`\`

MCP Server 是 API 客户端，不直接连接数据库。API 进程不得接收数据库 URL 环境变量。Worker 应作为独立进程或容器运行，并且只获得范围受限的密钥访问权限及必要的网络出口权限。

## 支持的数据库模式

| 数据库 | 原生事务模式 | 查询规划 | 自动执行 |
|---|---|---|---|
| MySQL 8 | \`START TRANSACTION READ ONLY\`，并在服务器端验证 | \`EXPLAIN FORMAT=JSON\` | 仅当所有低风险门禁均通过时允许 |
| PostgreSQL 16 | \`SET TRANSACTION READ ONLY\`，并在服务器端验证 | \`EXPLAIN (FORMAT JSON)\` | 仅当所有低风险门禁均通过时允许 |
| Oracle 19c | 将 \`SET TRANSACTION READ ONLY\` 作为事务中的第一条语句 | 跳过，因为 \`EXPLAIN PLAN\` 会写入 \`PLAN_TABLE\` | 禁用；必须由人工审核 |

这些是事务级控制，不能替代最小权限数据库账号。特别是，MySQL 只读事务仍可能允许写入临时表。数据库账号不得拥有 \`CREATE TEMPORARY TABLES\` 权限；SQL 策略仍必须拒绝临时表操作。

## 快速开始（本地开发）

环境要求：Python 3.11 或更高版本；使用容器运行时还需要 Docker Compose。

### 1. 获取代码并安装依赖

\`\`\`bash
git clone https://github.com/PenguinAlston/safe-db-mcp.git
cd safe-db-mcp
python -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
\`\`\`

### 2. 创建本地配置

\`\`\`bash
cp .env.example .env
cp config/datasources.example.yaml config/datasources.yaml
\`\`\`

编辑 \`.env\`，为以下两个变量分别生成独立且足够长的随机令牌：

- \`SAFEDB_AGENT_TOKEN\`
- \`SAFEDB_REVIEWER_TOKEN\`

将 \`SAFEDB_AGENT_ID\` 和 \`SAFEDB_REVIEWER_ID\` 设置为不同的身份标识。编辑 \`config/datasources.yaml\`，使其匹配实际 Schema 和表。不要将这两个本地配置文件提交到版本库。

每个数据源都应满足以下要求：

- \`allowed_tables\` 必须使用带 Schema 的表名（例如 \`public.orders\`）。
- \`allowed_agents\` 仅列出确实获准使用该数据源的 Agent ID。
- \`sensitive_columns\` 使用不带 Schema 的列名。
- 在该数据源通过集成测试前，保持 \`auto_execute: false\`。
- 使用权限范围尽可能小的数据库账号。

### 3. 仅在 Worker 环境中设置数据库 URL

使用以下 SQLAlchemy URL 格式，并通过本地密钥管理器或受保护的 Shell 环境提供凭证：

- MySQL：通过 \`MYSQL8_URL\` 提供，格式为 \`mysql+pymysql://...\`
- PostgreSQL：通过 \`PG16_URL\` 提供，格式为 \`postgresql+psycopg://...\`
- Oracle：通过 \`ORACLE19C_URL\` 提供，格式为 \`oracle+oracledb://...\`

绝不要将数据库 URL 放入 \`.env.example\`、API 容器、MCP 客户端配置、源代码管理、日志或提示词中。只有 Worker 可以接收这些密钥。

### 4. 启动服务

本地开发时，请在不同终端中分别启动 API 和 Worker。两者必须使用相同的 \`SAFEDB_DB_PATH\` 和数据源配置文件。

终端 1 — API：

\`\`\`bash
set -a
source .env
set +a
uvicorn safe_db_mcp.api:app --host 127.0.0.1 --port 8080
\`\`\`

终端 2 — Worker（从仅供 Worker 使用的密钥来源加载数据源 URL）：

\`\`\`bash
set -a
source .env
set +a
export SAFEDB_DB_PATH=./data/safedb.sqlite3
export SAFEDB_DATASOURCES_FILE=./config/datasources.yaml
# 仅注入此 Worker 所需的数据源 URL，例如从 Vault
# 或本地密钥管理器注入。不要将这些值加入 API 的环境变量。
read -r -s -p "PG16_URL: " PG16_URL
export PG16_URL
printf '\\n'
python -m safe_db_mcp.worker
\`\`\`

API 和 MCP 终端使用的共享 \`.env\` 中不得包含数据库 URL。Worker Shell 只能接收它实际需要的数据源密钥。使用 Compose 时，插值文件中的值只会传递给 Worker 服务，因为 API 服务没有声明数据库 URL 环境变量。

终端 3 — MCP Server：

\`\`\`bash
set -a
source .env
set +a
export SAFEDB_API_URL=http://127.0.0.1:8080
python -m safe_db_mcp.mcp_server
\`\`\`

MCP Server 默认使用 stdio。配置 MCP 客户端运行 \`python -m safe_db_mcp.mcp_server\`，并提供 \`SAFEDB_API_URL\` 和 \`SAFEDB_AGENT_TOKEN\`。不要向 MCP 客户端提供数据库 URL。

### Docker Compose 方式

\`\`\`bash
cp .env.example .env
cp config/datasources.example.yaml config/datasources.yaml
# 编辑 .env（仅包含 Agent/审核配置）和数据源 YAML。
# 从密钥管理器将仅供 Worker 使用的数据库 URL 导出到当前 Shell，然后执行：
docker compose up --build
\`\`\`

Compose 将 API 端口绑定到 loopback 地址，并且不会把数据库 URL 环境变量传递给 API 服务。Worker 会接收数据源 URL 环境变量。生产环境应使用 Secret Manager/Vault 身份替代基于环境变量的密钥注入，并限制 Worker 的网络出口。Compose 和 SQLite 仅用于本地开发。

## 提交并审核查询

API 地址为 \`http://127.0.0.1:8080\`。可通过 \`/docs\` 访问交互式 OpenAPI 文档。

提交查询时，请将数据源、表和列名替换为配置中实际存在的名称：

\`\`\`bash
curl --fail-with-body \\
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  http://127.0.0.1:8080/v1/queries \\
  -d '{
    "datasource_id": "pg16",
    "sql": "SELECT id, created_at FROM public.orders WHERE id = :order_id LIMIT 10",
    "purpose": "在支持调查中检查一条订单记录",
    "parameters": {"order_id": 123}
  }'
\`\`\`

API 会返回一个 proposal ID。Worker 会异步处理该提案。使用以下命令查询状态：

\`\`\`bash
curl --fail-with-body \\
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \\
  http://127.0.0.1:8080/v1/queries/<proposal-id>
\`\`\`

如果提案进入 \`PENDING_APPROVAL\` 状态，则需要由独立的审核身份检查审核队列：

\`\`\`bash
curl --fail-with-body \\
  -H "Authorization: Bearer $SAFEDB_REVIEWER_TOKEN" \\
  http://127.0.0.1:8080/v1/review/queue
\`\`\`

批准或拒绝提案时必须附上原因：

\`\`\`bash
curl --fail-with-body \\
  -X POST \\
  -H "Authorization: Bearer $SAFEDB_REVIEWER_TOKEN" \\
  -H "Content-Type: application/json" \\
  http://127.0.0.1:8080/v1/queries/<proposal-id>/decision \\
  -d '{"approve": true, "reason": "已审核查询范围和业务目的"}'
\`\`\`

Worker 执行获准的查询后，提交该请求的 Agent 可以获取受限结果：

\`\`\`bash
curl --fail-with-body \\
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \\
  http://127.0.0.1:8080/v1/queries/<proposal-id>/result
\`\`\`

请求者不能批准自己提交的查询。人工批准绝不能覆盖 SQL 校验、数据源授权、数据库目录检查、原生只读约束、硬性扫描限制或结果限制。

## API 和 MCP 接口

| 操作 | Endpoint / MCP 工具 | 身份验证 |
|---|---|---|
| 健康检查 | \`GET /health\` | 无 |
| 列出可访问的数据源 | \`GET /v1/datasources\` / \`list_datasources\` | Agent |
| 提交查询 | \`POST /v1/queries\` / \`prepare_query\` | Agent |
| 读取提案状态 | \`GET /v1/queries/{id}\` / \`get_query_status\` | 提交请求的 Agent |
| 读取成功结果 | \`GET /v1/queries/{id}/result\` / \`get_query_result\` | 提交请求的 Agent |
| 审核队列 | \`GET /v1/review/queue\` | Reviewer |
| 批准/拒绝 | \`POST /v1/queries/{id}/decision\` | Reviewer |

## 测试

\`\`\`bash
ruff check src tests
pytest -q
\`\`\`

CI 会在 Python 3.12 上运行这些命令。单元测试使用 Mock 和临时 SQLite 存储，不能证明真实数据库上的只读行为。

启用某个数据源前，必须针对一次性测试 Schema 运行该数据库专属的集成测试。至少应覆盖：

- DML、DDL、\`SELECT INTO\`、\`FOR UPDATE\`、多语句，以及解析器差异攻击载荷。
- 临时表操作和数据库账号的实际授权。
- 基表与视图、物化视图、外部表、外部访问表及同义词之间的区分。
- 连接池复用、事务回滚、会话变量重置和取消操作。
- 语句超时、墙钟时间超时、大行/LOB、结果字节截断和中断的结果流。
- 审批失效，以及 Worker 过期租约和 fencing 行为。

## 文档索引

- [Agent 与代码助手协作规范](AGENTS.md)
- [架构与提案生命周期](docs/ARCHITECTURE.md)
- [开发与贡献指南](CONTRIBUTING.md)
- [运维与密钥管理](docs/OPERATIONS.md)
- [数据库集成测试矩阵](docs/TESTING.md)
- [安全政策与生产检查清单](SECURITY.md)

## 已知生产阻塞项

- 在多副本或多 Worker 部署前，将 SQLite 替换为基于事务的 PostgreSQL 提案存储。
- 将静态 Bearer Token 身份验证替换为 OIDC/OAuth JWT 验证，包括 issuer/audience/scope 检查、按 Agent 映射身份，以及基于 MFA 的审核员身份验证。
- 增加静态数据加密、由 KMS 管理的密钥、数据保留/删除策略，以及不可篡改的外部审计存储。
- 增加列级授权、租户/行级过滤和输出脱敏。仅使用表白名单无法提供行级或列级数据保护。
- 增加速率限制、按主体配额、队列限制、指标、监控、主动查询取消，以及安全/渗透测试。
- 针对精确的数据库服务器和驱动版本，验证所有原生只读、超时、连接池和流式处理行为。
- 单个超大字段或 LOB 仍可能在应用层字节数限制生效前，就被数据库驱动完整加载到内存中。

启用任何数据源前，请先阅读 [SECURITY.md](SECURITY.md)。
