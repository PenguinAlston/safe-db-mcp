# 运维指南

本文档适用于本地和预生产环境。本项目目前尚未获准用于生产环境。

## 进程隔离

请将 API、Worker 和 MCP Server 作为独立进程运行。只有 Worker 可以获取数据库凭证。堡垒机临时凭证推荐通过 Worker 本地凭证管理 Socket 注入，不再依赖 URL 环境变量。

| 变量 | API | Worker | MCP |
|---|---:|---:|---:|
| `SAFEDB_AGENT_TOKEN` | 是 | 否 | 是 |
| `SAFEDB_REVIEWER_TOKEN` | 是 | 否 | 否 |
| `SAFEDB_DB_PATH` | 是 | 是 | 否 |
| `SAFEDB_DATASOURCES_FILE` | 是 | 是 | 否 |
| `SAFEDB_API_URL` | 否 | 否 | 是 |
| `MYSQL8_URL` / `PG16_URL` / `ORACLE19C_URL` | **否** | 仅旧式开发兼容模式 | **否** |

API 和 Worker 必须指向同一个提案存储和数据源配置。在当前原型中，这意味着二者使用同一个本地 SQLite 文件路径；使用容器时，需要挂载共享卷。

## Worker 设置

- `SAFEDB_WORKER_INTERVAL_SECONDS`：轮询间隔，默认值为 `1` 秒，必须为正数。
- `SAFEDB_WORKER_LEASE_SECONDS`：过期任务认领恢复租约，默认值为 `600` 秒，必须为正数。

租约时间应明显长于最长的分析或执行操作，包括连接获取、查询规划、查询执行和结果获取。租约过短会导致第二个 Worker 把实际上只是执行较慢的任务重新认领。Fencing token 可以阻止旧 Worker 发布过期状态/结果，但无法阻止已经开始的数据库工作重复执行。

## 提案状态与恢复

存储层按以下方式重新排队无人处理的任务：

| 被放弃时的状态 | 恢复后的状态 |
|---|---|
| `ANALYZING_WORKER` | `ANALYZING` |
| `EXECUTING` | `APPROVED` |

租约恢复会清除旧 fencing token，并以原子方式写入审计事件。使用旧 token 恢复运行的 Worker 无法写入完成状态或发布结果。当前 SQLite 存储仅适合单机开发，不要将其扩展到多主机部署。

## 健康检查与接口

- `GET /health` 仅用于检查 API 进程是否存活，不是数据库就绪探针。
- `GET /v1/datasources` 只列出当前已认证 Agent 获准访问的数据源。
- `GET /v1/review/queue` 需要 Reviewer Token。
- `GET /v1/queries/{id}/result` 仅允许提交该提案且执行成功的 Agent 访问。

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


## 企业目标部署：客户内网 Worker（尚未实现）

下一阶段的企业目标架构是 C1：云端运行 MCP Gateway、API、身份/审批/任务控制面；客户内网运行独立 Worker。详见 [ADR-0001](adr/0001-customer-network-worker.md)。

目标网络规则：

- Worker 仅主动向明确配置的云端 HTTPS 端点发起出站连接；云端不应向客户网络发起连接。
- 客户数据库和堡垒机只允许客户内网 Worker 所在安全域访问。
- 控制面 PostgreSQL 保持云端私网，仅由云端 API/调度服务访问。Worker 通过租户绑定的 HTTPS 任务协议领取/回传任务，不直接连接控制面数据库。
- 每个客户/信任域使用专属 Worker 实例或强隔离 Worker 池。
- Worker 到数据库的出站目标必须有主机、端口和 DNS 解析 allowlist，不能把 Worker 做成任意网络代理。

### 人工申请的 8 小时临时连接串

当前堡垒机没有自动化申请/续期 API，运行流程必须保留人工步骤：

1. 操作人登录堡垒机并人工申请临时连接串。
2. 操作人登录客户内网 Worker 主机，通过本地 CLI（无回显输入）或客户内网受控管理入口录入连接串；不得通过云端 MCP/API 上传。
3. 录入时提供堡垒机明确显示的到期时间。若堡垒机无法提供精确到期时间，只能按客户批准的保守 TTL 从本地录入时间计时，不能默认无限有效。
4. Worker 验证新凭证。成功后创建新连接池并切换活动版本；失败时保留当前有效版本并记录不含秘密的失败事件。
5. 到期前通知操作人；到期后进入 NEEDS_CREDENTIAL，暂停该数据源的新任务。不得使用已过期凭证反复重试。
6. 操作人重新申请并录入新凭证后恢复处理，无需修改非敏感数据源 YAML 或重启整个 Worker 服务。

凭证存储要求：

- 首选客户现有 Vault、OS 密钥存储或客户自管 KMS。
- 进程内存模式可用于首个可控版本，但 Worker 重启后需要重新录入。
- 不得把明文连接串放入 YAML、普通配置文件、命令行参数、环境变量、shell history、云端存储或日志。
- 如果增加本地加密持久化，解密密钥必须由独立的客户控制密钥设施提供，并经过单独安全评审。
- 审计只记录操作者、数据源 ID、凭证版本、录入/过期时间和结果；不得记录连接串、密码或秘密指纹。

兼容性前置检查：从**计划运行 Worker 的主机**测试堡垒机连接串是否可达。若连接串依赖操作者 PC 的 localhost 隧道、个人 IP 或交互式会话，则 Worker 必须部署在对应网络节点，或由客户提供受控内网隧道/代理。不能假设连接串可在另一台机器上直接复用。

C1 中 SQL、参数和受限查询结果默认仍可能经过云端。部署前必须由客户批准云端数据处理范围；禁止结果离开客户网络的客户应评估 C2/C3，而不能仅凭使用了内网 Worker 就声称满足该要求。

### 当前实现差距

本仓库目前仍使用本地 SQLite 原型存储、共享存储式 Worker 轮询、静态开发令牌和本地 stdio MCP。尚未实现 PostgreSQL 控制面队列、Worker 出站任务协议、Worker 工作负载身份、远程 MCP OAuth 或凭证录入/轮换接口。以上内容是目标设计，不应被当作当前可部署功能。


## Worker 本地临时凭证管理（已实现的第一阶段能力）

对于 `credential_source.type: local_manual` 的数据源，Worker 提供仅本机 Unix Socket 管理接口。凭证仅驻留 Worker 进程内存，不写入 SQLite、YAML、普通文件或云端。Worker 重启后状态回到 `MISSING)，必须重新由管理员录入。

```bash
# 查看所有数据源凭证状态（不输出 URL/密码）
docker compose exec worker python -m safe_db_mcp.credentials status

# 隐藏输入并确认；默认有效期 450 分钟，保留 30 分钟安全余量
docker compose exec worker python -m safe_db_mcp.credentials set pg16 --ttl-minutes 450

# 指定堡垒机精确到期时间，必须带时区
docker compose exec worker python -m safe_db_mcp.credentials set pg16 --expires-at 2026-11-08T02:00:00+08:00

# 撤销本地缓存的凭证
docker compose exec worker python -m safe_db_mcp.credentials clear pg16
```

凭证只通过隐藏式 CLI 输入，再通过 Worker Unix Socket 发送。Socket 目录权限为 0700、Socket 文件为 0600，并在支持的平台校验对端 UID。CLI 应以 Worker 服务用户运行。不要使用 `sudo` 或其他用户身份绕过该边界。

Worker 对本地凭证实施以下状态管理：

- `MISSING`：未录入；该数据源新任务保持排队。
- `ACTIVE`：仍有超过 30 分钟有效期；可处理任务。
- `EXPIRING`：剩余有效期不超过 30 分钟；继续处理，并输出一次状态告警。
- `NEEDS_CREDENTIAL`：已过期或被清除；Worker 清除内存中的 DSN 并暂停该数据源新任务。
- `INVALID`：保留给未来的验证状态；当前版本不把具体数据库连接错误转换为可见秘密或自动重试。

录入时优先输入堡垒机明确显示的到期时间。若无法确认精确时间，使用保守 TTL；默认示例 450 分钟，而不是用满 480 分钟。新 DSN 先做语法和方言检查，后续首次数据库连接会验证实际可用性。Worker 每个提案独立创建适配器/连接池，因此替换凭证只影响后续新任务；正在执行的任务不会被自动迁移，若堡垒机撤销旧连接，查询会按正常执行错误处理。

本阶段尚未实现桌面通知、邮件或云端告警推送。当前到期提醒体现为凭证状态和 Worker 日志告警，生产部署应将日志接入客户现有告警系统。
