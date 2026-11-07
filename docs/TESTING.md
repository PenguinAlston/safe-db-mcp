# Testing strategy

## Fast local checks

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

The GitHub Actions workflow runs the lint and unit-test commands on Python 3.12.

## What the current unit tests establish

The unit suite covers the SQL guard, configuration validation, authentication edge cases, risk assessment, proposal-store lease recovery/fencing, adapter selection, database-catalog response handling, and mocked statement ordering/cleanup.

It does **not** prove behavior on a live database server, actual privileges, driver cursor behavior, server-side cancellation, or network failure handling.

## Required live database matrix

Run each test against the exact server, SQLAlchemy dialect, DBAPI driver and version intended for deployment. Use a disposable schema and non-production credentials.

| Area | MySQL 8 / PyMySQL | PostgreSQL 16 / psycopg | Oracle 19c / python-oracledb |
|---|---|---|---|
| Native read-only activation | Verify transaction mode on the server | Verify transaction mode on the server | Verify first-statement `SET TRANSACTION READ ONLY` behavior |
| DML / DDL denial | Test ordinary tables and temporary-table exception | Test DML and DDL rejection | Test DML and DDL rejection |
| Object catalog | Base table vs view/temp table | relkind table/partition vs view/foreign table | table vs view/synonym/external table |
| Timeouts | statement timeout, read timeout, pool wait | statement timeout, connect timeout, pool wait | `call_timeout`, connect timeout, monotonic execution/fetch deadline |
| Connection hygiene | rollback, `max_execution_time` reset, incomplete SSCursor invalidation | rollback, `statement_timeout` transaction-local cleanup | rollback, `call_timeout` reset, cancellation |
| Result limits | row/byte cap and interrupted stream cleanup | row/byte cap and interrupted stream cleanup | row/byte cap and interrupted stream cleanup |
| Pool reuse | run safe query after success/failure/truncation | run safe query after success/failure | run safe query after success/failure/cancel |

## Security regression payloads

For each dialect, verify fail-closed behavior for:

- Multiple statements, DML/DDL, `SELECT INTO`, `FOR UPDATE`, lock clauses and forbidden command nodes.
- SQL comments and dialect-specific comment carriers, especially MySQL executable comments and `--1`.
- Quoted/ambiguous identifiers, schema/cross-catalog references and unapproved object names.
- Unsupported functions, wildcard projections, CTEs until safe name resolution exists, and side-effecting database features.
- Bind marker tokens inside quoted string forms that may be reinterpreted by SQLAlchemy's `text()` parser.
- Temporary objects and database-specific read-only transaction exceptions.

## Concurrency and lifecycle tests

- Reclaim a stale claim and verify the previous token can no longer change proposal state.
- Invalidate an approval after a risk snapshot change and verify no result can be published under the old approval.
- Kill a worker during analysis and execution, then verify recovery and audit records.
- Verify reviewer decision compare-and-swap under simultaneous approvals/rejections.
- Verify every proposal state transition and its audit event are committed together.

## Evidence required before enabling a datasource

Record the database server version, SQLAlchemy version, driver version, test schema, grants used, commands/test cases executed, pass/fail results, and any residual driver limitations. Do not infer live-server safety from mocked tests or from a successful `SELECT 1`.
