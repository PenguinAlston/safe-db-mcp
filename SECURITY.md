# Security policy and production checklist

This project is an early security prototype. Do not use privileged production database credentials with it yet.

## Native read-only enforcement

Every user query must run in a fresh, clean database transaction with native read-only mode enabled before the first user statement.

- MySQL 8: START TRANSACTION READ ONLY, then verify @@transaction_read_only = 1. MySQL still permits DML against TEMPORARY tables in read-only transactions. SQL policy must reject temporary objects, and the DB principal must not have CREATE TEMPORARY TABLES.
- PostgreSQL 16: SET TRANSACTION READ ONLY, then verify SHOW transaction_read_only is on. Roll back after result retrieval.
- Oracle 19c: SET TRANSACTION READ ONLY must be the first statement in the transaction. Oracle has no directly equivalent transaction_read_only status variable. Successful activation followed by a harmless SELECT is the runtime capability check; activation is repeated on every execution connection. This is not an account-level read-only grant.

Native read-only transactions do not replace least-privilege DB users, SQL AST validation, function allowlists, or network isolation. Do not allow user-defined functions, stored procedures, side-effecting extensions, or arbitrary external calls through SELECT.

## Non-negotiable controls

- API and MCP process must not receive database URL variables.
- Worker must have access only to the datasource secrets it needs.
- Only reviewed adapters with verified database-native read-only transactions may execute.
- SQL allowlist and read-only enforcement are mandatory even after human approval.
- Agent and reviewer identities must be distinct and independently authenticated.
- Reviewer decisions bind to the stored SQL/parameter digest and expire.
- No SQL or parameter values in application logs.
- Restrict Worker network egress to explicitly configured DB hosts and ports.
- Never enable a datasource until database-specific integration tests prove the read-only behavior.

## Known gaps

1. Auth uses single-principal static bearer tokens for local development only. Replace with OIDC JWT validation, issuer/audience/scope checks, per-Agent principal mapping and MFA-backed reviewer auth.
2. SQLite is not suitable for multi-replica workers. Move proposals and audit to PostgreSQL with atomic claim/locking semantics.
3. SQL table allowlists are additionally checked against database catalogs: only schema-qualified base tables are accepted. Views, materialized views, foreign tables and Oracle synonyms are rejected. Column-level policies, row/tenant predicates and result redaction remain outstanding.
4. Add encrypted proposal/result storage, KMS-managed keys, retention/deletion, and an immutable external audit sink.
5. Oracle row-scan estimates are unavailable; the planner is deliberately skipped to avoid PLAN_TABLE writes, so Oracle auto-execution stays disabled.
6. Add SQLGlot fuzzing, differential parser tests, denial-of-service tests, database integration tests and container/network penetration tests.
7. Add rate limits, per-principal quotas, bounded queue depth and active query cancellation. Worker claims now have a configurable stale-lease recovery timeout; configure it above the maximum analysis/execution duration.
8. Add result redaction and prevent sensitive data from reaching the model.
9. MySQL read-only transactions have engine-specific semantics including temporary tables; remove temporary-table creation privileges from the DB principal.
10. Worker uses SQLAlchemy pooling; production must test rollback/reset behavior and session-variable hygiene for selected drivers. Oracle `call_timeout` is per round-trip; the adapter additionally enforces a monotonic wall-clock budget around execute/fetch, but real Oracle integration tests are still required.
11. Streaming result retrieval reduces batch buffering, but one oversized row/LOB may still be materialized by the driver before the byte cap is applied.

## Incident response

Rotate all datasource credentials immediately if a Worker host, container, process dump, environment file or Secret Manager identity is suspected compromised. Do not place secrets in GitHub issues, logs or model prompts.
