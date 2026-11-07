# SafeDB-MCP

A guarded SQL query gateway for AI agents. The API and MCP server accept query requests; a separate worker owns database credentials and executes approved queries.

> **Project status: security prototype / early MVP. Not production-ready.** The CI suite uses unit tests and mocks. It does not prove database-server enforcement. Do not connect privileged production credentials.

## What it does

- Accepts one read-only query request at a time and validates it with a pinned SQLGlot parser.
- Restricts queries to configured datasource agents and schema-qualified table allowlists.
- Rejects non-SELECT statements, SQL comment carriers, ambiguous MySQL lexical forms, unsupported identifiers, unsafe functions, wildcard projections, and unsupported query constructs.
- Requires database-native read-only transactions for MySQL 8, PostgreSQL 16, and Oracle 19c.
- Estimates query cost where safe, applies row/byte/time limits, and routes queries for reviewer approval when risk is not low.
- Keeps database URLs out of the API and MCP processes. Only the worker receives datasource secrets.
- Uses worker fencing tokens so an expired worker cannot overwrite a newer worker's state or publish a stale result.

## Architecture

```text
Agent
  |
  v
MCP server -- HTTP + Agent token --> API ----> Proposal store
                                           ^          ^
                                           |          |
                                     Reviewer API    Worker
                                    separate token   |
                                      approve/deny   +--> DB secret --> Database
                                                     +--> revalidate/plan/execute
```

The MCP server is an API client; it does not connect to a database. The API process must not receive database URL variables. The worker should run as a separate process/container with narrowly scoped secret access and network egress.

## Supported database modes

| Database | Native transaction mode | Planning | Automatic execution |
|---|---|---|---|
| MySQL 8 | `START TRANSACTION READ ONLY` and server verification | `EXPLAIN FORMAT=JSON` | Only when all low-risk gates pass |
| PostgreSQL 16 | `SET TRANSACTION READ ONLY` and server verification | `EXPLAIN (FORMAT JSON)` | Only when all low-risk gates pass |
| Oracle 19c | `SET TRANSACTION READ ONLY` as the first transaction statement | Skipped because `EXPLAIN PLAN` writes `PLAN_TABLE` | Disabled; human approval required |

These are transaction-level controls, not a replacement for least-privilege database accounts. In particular, MySQL read-only transactions can still allow writes to temporary tables. The database principal must not have `CREATE TEMPORARY TABLES`; SQL policy must continue to reject temporary-table operations.

## Quick start (local development)

Requirements: Python 3.11+ and, for the container path, Docker Compose.

### 1. Get the code and install dependencies

```bash
git clone https://github.com/PenguinAlston/safe-db-mcp.git
cd safe-db-mcp
python -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

### 2. Create local configuration

```bash
cp .env.example .env
cp config/datasources.example.yaml config/datasources.yaml
```

Edit `.env` and generate two independent, long random tokens for:

- `SAFEDB_AGENT_TOKEN`
- `SAFEDB_REVIEWER_TOKEN`

Set `SAFEDB_AGENT_ID` and `SAFEDB_REVIEWER_ID` to distinct identities. Edit `config/datasources.yaml` to match your real schemas and tables. Do not commit either local file.

For each datasource:

- Keep `allowed_tables` schema-qualified (for example, `public.orders`).
- Set `allowed_agents` to the exact Agent IDs permitted to use it.
- Set `sensitive_columns` to unqualified column names.
- Set `auto_execute: false` until the datasource has passed its integration tests.
- Use a database account with the narrowest possible privileges.

### 3. Set database URLs only in the worker environment

Use the SQLAlchemy URL forms below, with credentials supplied through a local secret manager or a protected shell environment:

- MySQL: `mysql+pymysql://...` via `MYSQL8_URL`
- PostgreSQL: `postgresql+psycopg://...` via `PG16_URL`
- Oracle: `oracle+oracledb://...` via `ORACLE19C_URL`

Never put database URLs into `.env.example`, the API container, MCP client configuration, source control, logs, or prompts. The worker is the only component that should receive the secrets.

### 4. Start the services

For local development, start the API and worker in separate terminals. Both must use the same `SAFEDB_DB_PATH` and datasource configuration file.

Terminal 1 — API:

```bash
set -a
source .env
set +a
uvicorn safe_db_mcp.api:app --host 127.0.0.1 --port 8080
```

Terminal 2 — worker (load datasource URLs from a worker-only secret source):

```bash
set -a
source .env
set +a
export SAFEDB_DB_PATH=./data/safedb.sqlite3
export SAFEDB_DATASOURCES_FILE=./config/datasources.yaml
# Inject only the datasource URLs this worker needs, for example from Vault
# or a local secret manager. Do not add these values to the API's environment.
read -r -s -p "PG16_URL: " PG16_URL
export PG16_URL
printf '\\n'
python -m safe_db_mcp.worker
```

Keep database URLs out of the shared `.env` used for the API and MCP terminal commands. The worker shell should receive only the datasource secrets it needs. For Compose, values in the Compose interpolation file are passed only to the worker service because the API service does not declare DB URL variables.

Terminal 3 — MCP server:

```bash
set -a
source .env
set +a
export SAFEDB_API_URL=http://127.0.0.1:8080
python -m safe_db_mcp.mcp_server
```

The MCP server uses stdio by default. Configure your MCP client to launch `python -m safe_db_mcp.mcp_server` with `SAFEDB_API_URL` and `SAFEDB_AGENT_TOKEN`. Do not provide database URLs to the MCP client.

### Docker Compose alternative

```bash
cp .env.example .env
cp config/datasources.example.yaml config/datasources.yaml
# Edit .env (Agent/reviewer settings only) and the datasource YAML.
# Export worker-only DB URLs into this shell from your secret manager, then run:
docker compose up --build
```

Compose binds the API port to loopback and does not pass DB URL variables to the API service. The worker receives the datasource URL variables. For production, replace environment-based secret injection with a Secret Manager/Vault identity and restrict worker egress. Compose and SQLite are for local development only.

## Submit and review a query

The API is available at `http://127.0.0.1:8080`. Interactive OpenAPI documentation is available at `/docs`.

Submit a query (replace the datasource/table/column names with entries from your configuration):

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/v1/queries \
  -d '{
    "datasource_id": "pg16",
    "sql": "SELECT id, created_at FROM public.orders WHERE id = :order_id LIMIT 10",
    "purpose": "Inspect one order during support investigation",
    "parameters": {"order_id": 123}
  }'
```

The API returns a proposal ID. The worker processes the proposal asynchronously. Query status with:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \
  http://127.0.0.1:8080/v1/queries/<proposal-id>
```

If the proposal enters `PENDING_APPROVAL`, a separate reviewer identity checks the review queue:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_REVIEWER_TOKEN" \
  http://127.0.0.1:8080/v1/review/queue
```

Approve or reject a proposal with a reason:

```bash
curl --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $SAFEDB_REVIEWER_TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/v1/queries/<proposal-id>/decision \
  -d '{"approve": true, "reason": "Reviewed query scope and business purpose"}'
```

Once the worker has executed the approved query, the submitting Agent can retrieve its bounded result:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $SAFEDB_AGENT_TOKEN" \
  http://127.0.0.1:8080/v1/queries/<proposal-id>/result
```

The requester cannot approve their own query. Human approval never overrides SQL validation, datasource authorization, catalog checks, native read-only enforcement, hard scan limits, or result limits.

## API and MCP surface

| Operation | Endpoint / MCP tool | Authentication |
|---|---|---|
| Health | `GET /health` | None |
| List accessible datasources | `GET /v1/datasources` / `list_datasources` | Agent |
| Submit query | `POST /v1/queries` / `prepare_query` | Agent |
| Read proposal status | `GET /v1/queries/{id}` / `get_query_status` | Submitting Agent |
| Read successful result | `GET /v1/queries/{id}/result` / `get_query_result` | Submitting Agent |
| Review queue | `GET /v1/review/queue` | Reviewer |
| Approve/reject | `POST /v1/queries/{id}/decision` | Reviewer |

## Tests

```bash
ruff check src tests
pytest -q
```

CI runs those commands on Python 3.12. Unit tests use mocks and a temporary SQLite store; they do not establish read-only behavior on live databases.

Before enabling a datasource, run database-specific integration tests against a disposable test schema. At minimum test:

- DML, DDL, `SELECT INTO`, `FOR UPDATE`, multi-statement and comment/parser-differential payloads.
- Temporary-table operations and effective database grants.
- Base tables versus views, materialized views, foreign tables, external tables and synonyms.
- Connection-pool reuse, transaction rollback, session-variable reset and cancellation.
- Statement timeout, wall-clock timeout, large rows/LOBs, result byte truncation and interrupted streams.
- Approval invalidation and stale-worker lease/fencing behavior.

## Documentation

- [Agent and coding-assistant instructions](AGENTS.md)
- [Architecture and proposal lifecycle](docs/ARCHITECTURE.md)
- [Development and contribution guide](CONTRIBUTING.md)
- [Operations and secret handling](docs/OPERATIONS.md)
- [Database integration test matrix](docs/TESTING.md)
- [Security policy and production checklist](SECURITY.md)

## Known production blockers

- Replace SQLite with a transactional PostgreSQL-backed proposal store before multi-replica or multi-worker deployment.
- Replace static bearer tokens with OIDC/OAuth JWT validation, issuer/audience/scope checks, per-Agent identity mapping and MFA-backed reviewer authentication.
- Add encryption at rest, KMS-managed keys, retention/deletion policies and an immutable external audit sink.
- Add column-level authorization, tenant/row filters and output redaction. Table allowlists alone do not provide row-level or column-level data protection.
- Add rate limits, per-principal quotas, queue limits, metrics, monitoring, active query cancellation and security/penetration testing.
- Validate all native read-only, timeout, pooling and streaming behavior against exact server/driver versions.
- A single oversized value or LOB may be materialized by a database driver before the response byte cap can be applied.

See [SECURITY.md](SECURITY.md) before enabling any datasource.
