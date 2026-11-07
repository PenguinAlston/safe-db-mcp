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

```text
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
```

MCP Server 是 API 客户端，不直接连接数据库。API 进程不得接收数据库 URL 环境变量。Worker 应作为独立进程或容器运行，并且只获得范围受限的密钥访问权限及必要的网络出口权限。

## 支持的数据库模式

| 数据库 | 原生事务模式 | 查询规划 | 自动执行 |
|---|---|---|---|
| MySQL 8 | `SET SESSION transaction_read_only=ON` 后 `START TRANSACTION`，并验证 `@@transaction_read_only=1` | `EXPLAIN FORMAT=JSON` | 仅当所有低风险门禁均通过时允许 |
| PostgreSQL 16 | `SET TRANSACTION READ ONLY`，并在服务器端验证 | `EXPLAIN (FORMAT JSON)` | 仅当所有低风险门禁均通过时允许 |
| Oracle 19c | 将 `SET TRANSACTION READ ONLY` 作为事务中的第一条语句 | 跳过，因为 `EXPLAIN PLAN` 会写入 `PLAN_TABLE` | 禁用；必须由人工审核 |

这些是事务级控制，不能替代最小权限数据库账号。特别是，MySQL 只读事务仍可能允许写入临时表。数据库账号不得拥有 `CREATE TEMPORARY TABLES` 权限；SQL 策略仍必须拒绝临时表操作。

## 快速开始（本地开发）

环境要求：Python 3.11 或更高版本；使用容器运行时还需要 Docker Compose。

### 1. 获取代码并安装依赖

```bash
git clone https://github.com/PenguinAlston/safe-db-mcp.git
cd safe-db-mcp
python -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

### 2. 创建本地配置

```bash
cp .env.example .env
cp config/datasources.example.yaml config/datasources.yaml
```

编辑 `.env`，为以下两个变量分别生成独立且足够长的随机令牌：

- `SAFEDB_AGENT_TOKEN`
- `SAFEDB_REVIEWER_TOKEN`

将 `SAFEDB_AGENT_ID` 和 `SAFEDB_REVIEWER_ID` 设置为不同的身份标识。编辑 `config/datasources.yaml`，使其匹配实际 Schema 和表。不要将这两个本地配置文件提交到版本库。

每个数据源都应满足以下要求：

- `allowed_tables` 必须使用带 Schema 的表名（例如 `public.orders`）。
- `allowed_agents` 仅列出确实获准使用该数据源的 Agent ID。
- `sensitive_columns` 必须是列名字符串列表，例如 `[payment_token]`；数值限额必须是 YAML 整数，不接受布尔值、小数或数字字符串。
- MySQL 仅接受 InnoDB 基表；PostgreSQL 拒绝分区父表、有继承子表的表，以及包含非 `pg_catalog` 基础类型列的表。
- 在该数据源通过集成测试前，保持 `auto_execute: false`。
- 使用权限范围尽可能小的数据库账号。

### 3. 配置 Worker 本地临时凭证

推荐的企业/堡垒机配置使用 `credential_source.type: local_manual`，见 `config/datasources.example.yaml`。YAML 只保存数据源元数据与查询策略，不保存 `url_env` 或临时连接串。

Worker 启动后会同时提供本机 Web 管理面板和 Unix Socket 管理接口。推荐日常通过浏览器面板管理凭证；CLI 仍可用于无图形环境的服务器：

```bash
# 首次设置 Web 面板密码（仅需执行一次；Worker 重启后需重新设置）
docker compose exec worker python -m safe_db_mcp.credentials ui-password set

# 浏览器打开 http://127.0.0.1:8090，登录后即可查看状态、录入/替换/清除凭证

# CLI 方式：查看数据源凭证状态，不显示任何秘密
docker compose exec worker python -m safe_db_mcp.credentials status

# 默认按 7 小时设置保守有效期（为人工申请、复制和录入过程预留 1 小时余量）
docker compose exec worker python -m safe_db_mcp.credentials set pg16 --ttl-minutes 420

# 或填写堡垒机明确显示的过期时间（必须包含时区）
docker compose exec worker python -m safe_db_mcp.credentials set pg16 --expires-at 2026-11-08T02:00:00+08:00

# 清除某个数据源的本地凭证
docker compose exec worker python -m safe_db_mcp.credentials clear pg16
```

CLI 使用无回显输入并要求再次确认。连接串通过 Worker 专属的本地 Unix Socket 送入 Worker 进程内存，不经过云端 API/MCP，不写入 YAML、环境变量或持久化数据库。Worker 会先使用候选连接串验证数据库原生只读能力和配置表目录，成功后才替换当前凭证；验证失败不会覆盖旧凭证。只有与 Worker 进程相同的操作系统用户可以访问 Socket。默认采用纯内存存储：Worker 重启后需重新录入凭证。到期后 Worker 清除内存中的凭证并暂停该数据源的新任务；凭证缺失时任务保持原状态排队，不会被当成查询失败处理；但提案仍受默认 15 分钟自身有效期约束，过期后会按正常提案生命周期失效。到期前 30 分钟进入 `EXPIRING` 状态并写入不含秘密的日志。

**连接串兼容性前置检查：** 必须从运行 Worker 的实际主机验证堡垒机连接串可用。如果它依赖操作者电脑上的 localhost SSH 隧道、个人 IP 或桌面会话，不能直接复制到另一台服务器使用。

旧式 `url_env` 仍为本地开发兼容模式，但不要用于人工临时凭证；该模式下数据库 URL 只注入 Worker 进程。

### 4. 启动服务

本地开发时，请在不同终端中分别启动 API 和 Worker。两者必须使用相同的 `SAFEDB_DB_PATH` 和数据源配置文件。

终端 1 — API：

```bash
set -a
source .env
set +a
uvicorn safe_db_mcp.api:app --host 127.0.0.1 --port 8080
```

终端 2 — Worker（不注入数据库 URL；启动本机管理面板和 Socket）：

```bash
export SAFEDB_DB_PATH=./data/safedb.sqlite3
export SAFEDB_DATASOURCES_FILE=./config/datasources.yaml
# local_manual 模式不需要数据库 URL 环境变量
python -m safe_db_mcp.worker
```

终端 4 — 首次设置本机管理面板密码（同 Worker OS 用户）：

```bash
python -m safe_db_mcp.credentials ui-password set
```

然后浏览器访问 `http://127.0.0.1:8090`。管理面板默认仅监听 loopback。

API 使用的 `.env` 中不得包含数据库 URL。Worker 不加载包含 Agent/Reviewer Token 的 `.env`，只注入存储路径和数据源配置。堡垒机临时连接串通过 Worker 本地凭证 CLI 录入。

终端 3 — MCP Server（使用未加载 API 或 Worker 密钥的新终端，不要 `source .env`）：

```bash
export SAFEDB_API_URL=http://127.0.0.1:8080
read -r -s -p "Agent Token: " SAFEDB_AGENT_TOKEN
export SAFEDB_AGENT_TOKEN
printf '\\n'
python -m safe_db_mcp.mcp_server
```

MCP Server 默认使用 stdio。配置 MCP 客户端运行 `python -m safe_db_mcp.mcp_server`，并提供 `SAFEDB_API_URL` 和 `SAFEDB_AGENT_TOKEN`。不要向 MCP 客户端提供数据库 URL。

### Docker Compose 方式

```bash
cp .env.example .env
cp config/datasources.example.yaml config/datasources.yaml
# 编辑 .env（仅包含 Agent/审核配置）和数据源 YAML。
# 启动 API 和 Worker；先不要向任何容器注入数据库 URL
docker compose up --build -d
# 设置 Worker 本地管理面板密码
docker compose exec worker python -m safe_db_mcp.credentials ui-password set

# 然后浏览器访问 http://127.0.0.1:8090，登录后录入 pg16 临时连接串
```

Linux 使用绑定挂载时，需预先创建 `data` 目录，并确保容器 UID/GID 10001 可写、其他非授权用户不可读写；否则 SQLite 无法初始化。不要通过将目录设为全员可写来绕过权限错误。Worker 的 `worker-run` 命名卷由镜像预设为 UID 10001 的私有目录。

Compose 将 API 和 Worker 管理面板端口均绑定到宿主机 loopback 地址。API 和 Worker 均不接收数据库 URL 环境变量。Worker 从本机凭证管理器读取进程内存中的临时凭证。当前保留 SQLite，适用于 API 与单个 Worker 同机运行的单节点部署；数据库文件必须位于本机可靠磁盘，不得放在 NFS/SMB 等网络共享文件系统。此部署形态仍是安全原型，不是多节点 SaaS。

### Docker Compose 中的 Worker 凭证管理

Worker 使用独立的命名卷保存本地 Unix Socket 运行目录，不包含凭证数据。管理面板默认仅映射到宿主机 `127.0.0.1:8090`，浏览器访问 `http://127.0.0.1:8090`。首次需在 Worker 容器内设置面板密码：`docker compose exec worker python -m safe_db_mcp.credentials ui-password set`。密码哈希和登录会话均仅保存在 Worker 进程内存中；Worker 重启后需重新设置。不要将端口改为公网或局域网监听，也不要通过云端 MCP 工具提交数据库连接串。


## 提交并审核查询

API 地址为 `http://127.0.0.1:8080`。可通过 `/docs` 访问交互式 OpenAPI 文档。

提交查询时，请将数据源、表和列名替换为配置中实际存在的名称：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/v1/queries \
  -d '{
    "datasource_id": "pg16",
    "sql": "SELECT id, created_at FROM public.orders WHERE id = :order_id LIMIT 10",
    "purpose": "在支持调查中检查一条订单记录",
    "parameters": {"order_id": 123}
  }'
```

API 会返回一个 proposal ID。Worker 会异步处理该提案。使用以下命令查询状态：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \
  http://127.0.0.1:8080/v1/queries/<proposal-id>
```

如果提案进入 `PENDING_APPROVAL` 状态，则需要由独立的审核身份检查审核队列：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_REVIEWER_TOKEN" \
  http://127.0.0.1:8080/v1/review/queue
```

批准或拒绝提案时必须附上原因和刚刚审核的队列项中的 `review_token`。风险重评后此版本令牌会更换；旧页面的决策返回 409，必须重新审核。队列支持 `?limit=100&offset=0` 分页，并清理过期待审记录：

```bash
curl --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $SAFEDB_REVIEWER_TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/v1/queries/<proposal-id>/decision \
  -d '{"approve": true, "reason": "已审核查询范围和业务目的", "review_token": "<复制当前队列项中的32位review_token>"}'
```

Worker 执行获准的查询后，提交该请求的 Agent 可以获取受限结果：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \
  http://127.0.0.1:8080/v1/queries/<proposal-id>/result
```

请求者不能批准自己提交的查询。人工批准绝不能覆盖 SQL 校验、数据源授权、数据库目录检查、原生只读约束、硬性扫描限制或结果限制。

## API 和 MCP 接口

| 操作 | Endpoint / MCP 工具 | 身份验证 |
|---|---|---|
| 健康检查 | `GET /health` | 无 |
| 列出可访问的数据源 | `GET /v1/datasources` / `list_datasources` | Agent |
| 提交查询 | `POST /v1/queries` / `prepare_query` | Agent |
| 读取提案状态 | `GET /v1/queries/{id}` / `get_query_status` | 提交请求的 Agent |
| 读取成功结果 | `GET /v1/queries/{id}/result` / `get_query_result` | 提交请求的 Agent |
| 审核队列 | `GET /v1/review/queue` | Reviewer |
| 批准/拒绝 | `POST /v1/queries/{id}/decision` | Reviewer |

## 测试

```bash
ruff check src tests
pytest -q
```

CI 会在 Python 3.12 上运行这些命令。单元测试使用 Mock 和临时 SQLite 存储，不能证明真实数据库上的只读行为。

真实数据库回归位于 `tests/test_database_integration.py`。在隔离的测试进程中注入 `SAFEDB_TEST_MYSQL_URL` / `SAFEDB_TEST_POSTGRES_URL` 后运行 `pytest tests/test_database_integration.py -q`；未配置的数据库会明确跳过。目录边界与高权限账号只读拒写测试另需一次性测试库的 `SAFEDB_TEST_MYSQL_ADMIN_URL` / `SAFEDB_TEST_POSTGRES_ADMIN_URL`，不能使用生产账号。PostgreSQL 测试对象在事务结束时回滚，MySQL 测试创建的唯一命名表在结束时删除。

过滤扫描、重复扫描和无法可靠解读的执行计划不提供可自动执行的扫描估算，需人工审核。估算不是运行时扫描硬配额。Decimal 与非有限浮点数以字符串返回，避免精度丢失或生成无效 JSON。

启用某个数据源前，必须针对一次性测试 Schema 运行该数据库专属的集成测试。至少应覆盖：

- DML、DDL、`SELECT INTO`、`FOR UPDATE`、多语句，以及解析器差异攻击载荷。
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


## 企业目标架构方向

下一阶段以“云端控制面 + 客户内网 Worker（C1）”为企业目标架构。客户内网 Worker 负责访问数据库并保管数据库临时凭证；云端负责身份、审批和任务控制。当前堡垒机若只能人工签发约 8 小时凭证，管理员将在客户内网本地录入，Worker 管理到期提醒、停单和连接池切换，不需要频繁修改 YAML。**该远程 Worker 与凭证管理能力尚未实现**，项目仍为安全原型。详见 [架构决策 ADR-0001](docs/adr/0001-customer-network-worker.md)。
