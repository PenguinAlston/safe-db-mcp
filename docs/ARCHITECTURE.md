# Architecture

## Components

| Component | Responsibility | Database credentials |
|---|---|---|
| Agent / MCP client | Submit SELECT intent; poll status and fetch own results | No |
| MCP server | Thin authenticated HTTP client for the API | No |
| API | Authenticate Agent/reviewer, validate request, persist proposal, expose status/review endpoints | No |
| Worker | Claim proposals, resolve datasource secret, validate again, plan, execute in native read-only transaction, persist result | Yes, scoped to required datasources |
| Proposal store | Persist proposal lifecycle and audit events | No database-target credentials |
| Reviewer | Review query, purpose and risk snapshot; approve/reject with reason | Reviewer token only |

## Proposal lifecycle

```text
Agent submits
     |
     v
 ANALYZING
     |
     | worker atomically claims with unique fencing token
     v
ANALYZING_WORKER
     |
     +---- validation/object/read-only failure ----> BLOCKED
     |
     +---- low risk and all auto gates pass -------> EXECUTING
     |
     +---- human review required ------------------> PENDING_APPROVAL
                                                       |
                                                reviewer decision
                                                  /       \
                                             REJECTED    APPROVED
                                                           |
                                                   worker claims with
                                                   a new fencing token
                                                           |
                                                      EXECUTING
                                                           |
                                            revalidate/replan/risk compare
                                                  /             \
                                         BLOCKED or review     execute
                                                                 |
                                                             SUCCEEDED
```

Expired claims are recovered to `ANALYZING` or `APPROVED`. Lease recovery clears the previous fencing token. Every worker write checks the current token and expected state so an old process cannot publish a result after the claim has moved.

## Execution gates

Before executing user SQL, the worker must:

1. Check proposal expiry, requester permission and stored query/parameter hash.
2. Re-parse SQL under the selected dialect.
3. Verify native read-only support on a fresh connection.
4. Resolve every referenced object in the database catalog and accept only supported base-table objects.
5. Run a bounded EXPLAIN where safe; Oracle EXPLAIN PLAN is deliberately skipped because it writes PLAN_TABLE.
6. Recalculate risk and compare the approval snapshot. Any change invalidates the approval.
7. Re-check that the worker still owns the fencing token immediately before execution.
8. Execute inside a native read-only transaction with time/resource limits.
9. Bound rows and serialized result bytes, reset or invalidate the connection, and publish the result only if the claim is still current.

## Persistence

SQLite is the development store. Its transactions make the current single-host claim/CAS behavior testable, but SQLite is not the production multi-replica queue. A PostgreSQL-backed production store must retain atomic claim, fencing token validation, lease recovery, and transactionally coupled audit writes.

## Security boundary

Native read-only transactions constrain the current transaction; they do not make a privileged account safe. Use database accounts with least privilege, remove MySQL temporary-table creation privileges, restrict network egress, and keep the API/MCP isolated from database secrets.

Table allowlists are not row-level or column-level controls. Sensitive columns, tenant/row predicates, data redaction, credential rotation, rate limits, external immutable audit storage, and production-grade identity are still required before production.
