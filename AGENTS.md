# AGENTS.md

Instructions for AI coding agents and human contributors working in this repository.

## Project goal and trust boundaries

SafeDB-MCP is a guarded SQL query gateway. Treat the following as hard architectural boundaries:

1. The Agent/MCP process and API must never receive database credentials or open database connections.
2. Only the separate worker process may resolve datasource secrets and connect to databases.
3. A human approval is an additional gate, never an override for SQL validation, datasource authorization, catalog allowlists, native read-only mode, hard limits, or result limits.
4. Fail closed whenever parsing, authorization, object resolution, native read-only verification, or timeout enforcement is uncertain.
5. This repository is a security prototype, not a production-ready database isolation boundary.

## Mandatory change rules

- Read `SECURITY.md`, the relevant adapter, `security/sql_guard.py`, `service.py`, `store.py`, and the affected tests before changing security-sensitive code.
- Keep database URLs out of API/MCP configuration, logs, test fixtures committed to the repository, docs, and examples. Use obviously fake placeholders in tests.
- Never log SQL text, bind values, connection URLs, credentials, driver exception messages, or query result content.
- Do not weaken SQL AST restrictions, function allowlists, datasource/Agent allowlists, catalog object checks, native read-only checks, reviewer separation, or resource limits just to make a test pass.
- Every accepted SQL form must be safe under both the SQLGlot parser and the SQLAlchemy/driver/server lexical interpretation. Add parser-differential regression tests when changing SQL acceptance.
- Keep table identifiers schema-qualified, simple and unquoted unless a dialect-aware resolver is implemented and tested. Avoid case-folding or string rewrites that can map one database object to another.
- Worker state transitions and result publication must use the active fencing token and expected status. Never add an unconditional worker write that can publish after lease recovery.
- Proposal state changes and their audit events must be committed atomically.
- Any database session state (read-only mode, statement timeout, session variables, cursor state) must be reset before returning a connection to the pool. Invalidate the connection if safe reset cannot be guaranteed.
- Preserve bounded execution: connection acquisition, database round trips, query execution, result rows and result bytes need explicit limits.
- Do not claim an integration test passed unless it ran against the actual database and driver versions.

## Database adapter requirements

For any new or modified adapter:

- Implement explicit native read-only transaction activation and verify the server-side state where the database supports verification.
- Activate read-only mode before the first user SQL statement. Oracle has a strict first-statement requirement; do not add pings or metadata SQL before it in that transaction.
- Treat metadata/catalog lookup failure as denial.
- Reject views, synonyms, materialized views, foreign/external objects unless a documented, separately reviewed policy explicitly allows them.
- Apply timeouts to planning and catalog checks as well as execution.
- Test connection-pool reuse and cleanup after success, timeout, cancellation, truncation and exceptions.
- Document dialect-specific limitations in `SECURITY.md` and `README.md`.

## Worker and store requirements

- Claims must be atomic and return a unique fencing token.
- Lease recovery must invalidate the old token.
- Every completion/failure/block transition must compare both the token and expected status.
- A worker that loses its claim must not update proposal state, audit completion, or publish results.
- State and corresponding audit record must be committed in one transaction.
- SQLite is a local development store only. Any production store must preserve the fencing/CAS behavior with transactional locking.

## Tests and local checks

Run these before opening or updating a PR:

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

Add tests for both allowed behavior and rejection cases. Security tests should include malformed SQL, comment carriers, quoted identifiers, bind marker edge cases, unauthorized objects, approval races, expired worker claims and connection reset behavior.

Live database tests are separate from unit tests. When live database services are not available, state that clearly and leave the production-readiness claim unchanged.

## Documentation and review

- Update `README.md` when user-visible setup, configuration, API, or operating behavior changes.
- Update `SECURITY.md` whenever a security control or known gap changes.
- Keep examples runnable, use placeholder values, and never include a real secret.
- Keep changes focused. Explain security assumptions and residual risks in the PR description.
- Do not merge or deploy unless the task explicitly requests it.
