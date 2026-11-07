# SafeDB-MCP

A guarded SQL gateway for AI agents. **Status: early MVP / security prototype. Not production-ready.**

The API and MCP process must not receive database credentials. Only the isolated worker should resolve datasource secrets and execute approved queries.

## Native read-only adapters

- MySQL 8: starts every user query in START TRANSACTION READ ONLY and verifies @@transaction_read_only.
- PostgreSQL 16: starts every user query in SET TRANSACTION READ ONLY and verifies SHOW transaction_read_only.
- Oracle 19c: starts every user query with SET TRANSACTION READ ONLY as the first transaction statement. Oracle has no directly equivalent transaction_read_only status variable. Oracle EXPLAIN PLAN is skipped because it writes PLAN_TABLE; unknown estimates force human approval and auto-execution is disabled for Oracle.
- Other database dialects fail closed.

These are transaction-level protections, not substitutes for database users with least privilege. MySQL read-only transactions still permit writes to TEMPORARY tables; the DB principal must not have CREATE TEMPORARY TABLES, and SQL policy must continue to reject temp-table operations.

## Security model

- Agent submits a free-form SELECT, purpose, datasource ID and bind parameters.
- API validates SQL through SQLGlot AST, applies datasource table allowlists and datasource-specific Agent allowlists, then stores a proposal.
- A separate worker resolves credentials, verifies native read-only support, estimates query cost where safe, and calculates risk.
- LOW risk auto-executes only if every gate passes. MEDIUM/HIGH requires a separate human reviewer.
- Approval binds to the SQL + parameter hash, requester and expiry. The worker reparses, rechecks native read-only mode and replans before execution.
- Results are streamed one row at a time and bounded by row count and response bytes (a single oversized database value may still be materialized by the driver). Exact scanned rows are reported only when an adapter has a true runtime metric.
- Before planning and again before execution, the worker resolves every allowlisted object against database catalogs and accepts only base tables; views, materialized views, foreign tables and Oracle synonyms fail closed.
- Oracle applies a monotonic end-to-end time budget around execution and fetches, in addition to per-round-trip `call_timeout`.
- Agent never receives DB URLs, usernames, passwords or secret references. MCP has no direct database connection.

## Quick start (local development only)

Python 3.11+.

1. Install development dependencies with pip install -e ".[dev]".
2. Copy .env.example to a local environment file and set long random development tokens. Do not commit it.
3. Copy config/datasources.example.yaml to config/datasources.yaml, replace sample table names with exact allowlists, and set allowed_agents to your configured Agent ID.
4. Configure MYSQL8_URL, PG16_URL and ORACLE19C_URL only in the isolated worker environment or its Secret Manager identity. Use SQLAlchemy URL schemes mysql+pymysql://, postgresql+psycopg:// and oracle+oracledb:// respectively.
5. Start the API with SAFEDB_AGENT_TOKEN, SAFEDB_AGENT_ID, SAFEDB_REVIEWER_TOKEN, SAFEDB_REVIEWER_ID and SAFEDB_DB_PATH in its environment. Do not give the API any DB URL variables.
6. Start the worker separately with SAFEDB_DB_PATH, SAFEDB_DATASOURCES_FILE and only the datasource secrets it needs.
7. Start MCP with SAFEDB_API_URL and the matching SAFEDB_AGENT_TOKEN.

The MVP authenticator uses one Agent token and one reviewer token. Replace it with OIDC/OAuth JWT validation, audience/scope checks, per-Agent identity mapping and MFA-backed reviewer identity before remote or multi-user production use.

## Deployment boundary

The Docker Compose file intentionally does not inject database URL variables into the API container. Only the worker should receive datasource secrets. Restrict worker network egress to approved database endpoints. Prefer a Secret Manager/Vault integration over environment variables in production.

## Production blockers

- SQLite is single-instance development storage. Use PostgreSQL with transactional row locking for multiple workers.
- Add encryption at rest for proposal SQL, parameters and results.
- Current policy is table-level. Add column-level authorization, tenant/row filters, view dependency resolution and output redaction.
- Validate native read-only enforcement against exact server and driver versions using integration tests and a dedicated test schema. Unit tests do not prove server-side enforcement.
- Oracle scan estimates are not available in this version; manual approval is mandatory and Oracle auto-execution is disabled.
- Add rate limits, quotas, queue limits, active query cancellation, immutable external audit sink, monitoring and penetration tests. Worker claims now have a configurable stale-lease recovery timeout; set it comfortably above the maximum query/analysis duration.
- EXPLAIN values are estimates. Exact runtime scanned rows remain unavailable for these adapters.
- Human approval never overrides the hard SQL allowlist or native read-only enforcement.

## Tests

Run pytest. Unit tests cover adapter selection and statement ordering, not live database enforcement. Run integration tests against MySQL 8, Oracle 19c and PostgreSQL 16 before enabling production datasources.

See SECURITY.md for the full checklist.
