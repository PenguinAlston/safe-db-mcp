# Security policy and production checklist

This project is an early security prototype. Do not use privileged production database credentials with it yet.

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
3. Table-level allowlist only; implement column-level policies, row/tenant predicates and view dependency resolution.
4. Add encrypted proposal/result storage, KMS-managed keys, retention/deletion, and an immutable external audit sink.
5. Implement and test each additional database adapter against exact server and driver versions.
6. Add SQLGlot fuzzing, differential parser tests, denial-of-service tests, database integration tests and container/network penetration tests.
7. Add rate limits, per-principal quotas, bounded queue depth, cancellation and worker lease recovery.
8. Add result redaction and prevent sensitive data from reaching the model.
9. MySQL read-only transactions have engine-specific semantics (including temporary tables); do not widen the SQL grammar without dialect-specific review.
10. Worker uses SQLAlchemy pooling; production must test rollback/reset behavior and session-variable hygiene for the selected drivers.

## Incident response

Rotate all datasource credentials immediately if a Worker host, container, process dump, environment file or Secret Manager identity is suspected compromised. Do not place secrets in GitHub issues, logs or model prompts.
