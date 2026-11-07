# SafeDB-MCP

A guarded SQL gateway for AI agents. **Status: early MVP / security prototype. Not production-ready.**

The API and MCP process must not receive database credentials. Only the isolated worker should resolve datasource secrets and execute approved queries. PostgreSQL and MySQL adapters are included as an initial implementation; all other dialects fail closed until reviewed adapters and integration tests are added.

## Security model

- Agent submits a free-form SELECT, purpose, datasource ID and bind parameters.
- API validates SQL through SQLGlot AST, applies the datasource table allowlist and datasource-specific Agent allowlist, then stores a proposal.
- A separate worker resolves credentials, verifies database-native read-only mode, runs EXPLAIN and calculates risk.
- LOW risk auto-executes only if every gate passes. MEDIUM/HIGH requires a separate human reviewer.
- Approval binds to the SQL + parameter hash, requester and expiry. The worker reparses, rechecks read-only mode and replans before execution.
- Results are bounded by row count and response bytes. Exact scanned rows are reported only when an adapter has a true runtime metric.
- Agent never receives DB URLs, usernames, passwords or secret references. MCP has no direct database connection.

## Included adapters

- PostgreSQL: read-only transaction verification and JSON EXPLAIN.
- MySQL: read-only transaction verification and JSON EXPLAIN.
- Oracle / SQL Server / others: extension points only, deliberately fail closed until separate reviewed adapters and integration tests exist.

## Quick start (local development only)

Python 3.11+.

1. Install: pip install -e dev dependencies.
2. Copy .env.example to a local environment file and set long random development tokens. Do not commit it.
3. Copy config/datasources.example.yaml to config/datasources.yaml, replace sample table names with exact allowlists, and set allowed_agents to your configured Agent ID.
4. Start the API with SAFEDB_AGENT_TOKEN, SAFEDB_AGENT_ID, SAFEDB_REVIEWER_TOKEN, SAFEDB_REVIEWER_ID and SAFEDB_DB_PATH in its environment. Do not give the API any DB URL variables.
5. Start the worker separately with SAFEDB_DB_PATH, SAFEDB_DATASOURCES_FILE and the DB URL variables or Secret Manager identity needed by that worker only.
6. Start MCP with SAFEDB_API_URL and the matching SAFEDB_AGENT_TOKEN.

The MVP authenticator uses one Agent token and one reviewer token. The MCP process sends its Agent token to the API; deploy a distinct MCP instance/token per Agent identity. Replace this development authenticator with OIDC/OAuth JWT validation, audience/scope checks, per-Agent identity mapping and MFA-backed reviewer identity before remote or multi-user production use.

## Deployment boundary

The Docker Compose file intentionally does not inject database URL variables into the API container. Only the worker should receive datasource secrets. Restrict worker network egress to approved database endpoints. Prefer a Secret Manager/Vault integration over environment variables in production.

## Production blockers

- SQLite is single-instance development storage. Use PostgreSQL with transactional row locking for multiple workers.
- Add encryption at rest for proposal SQL, parameters and results; these can contain sensitive business data.
- Current policy is table-level. Add column-level authorization, tenant/row filters, view dependency resolution and output redaction.
- Verify read-only behavior against exact database server and driver versions. A read-only transaction is not the same as a read-only account and has engine-specific limitations.
- The MySQL adapter must be validated against your exact MySQL/MariaDB version, especially temporary-table and session behavior.
- Oracle, SQL Server and other dialects remain blocked until a complete native adapter is implemented and tested.
- Add rate limits, quotas, queue limits, cancellation, worker lease recovery, immutable external audit sink, monitoring and penetration tests.
- EXPLAIN values are estimates. This MVP reports actual returned rows, bytes and duration; scanned rows remain null unless a true runtime metric is implemented.
- Human approval never overrides the hard SQL allowlist or read-only enforcement.

## Tests

Run pytest. Tests cover AST rejection and risk gates, not database-native enforcement. Run integration tests against every enabled database/version before deployment.

See SECURITY.md for the full checklist.
