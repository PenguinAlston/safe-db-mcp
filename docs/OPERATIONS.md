# Operations guide

This document is for local and pre-production operation. The project is not approved for production use.

## Process separation

Run the API, worker and MCP server as separate processes. Only the worker gets database URL variables.

| Variable | API | Worker | MCP |
|---|---:|---:|---:|
| `SAFEDB_AGENT_TOKEN` | Yes | No | Yes |
| `SAFEDB_REVIEWER_TOKEN` | Yes | No | No |
| `SAFEDB_DB_PATH` | Yes | Yes | No |
| `SAFEDB_DATASOURCES_FILE` | Yes | Yes | No |
| `SAFEDB_API_URL` | No | No | Yes |
| `MYSQL8_URL` / `PG16_URL` / `ORACLE19C_URL` | **No** | Yes, only required URLs | **No** |

The API and worker must point to the same proposal store and datasource configuration. In the current prototype, that means the same local SQLite file path and a shared mounted volume when containers are used.

## Worker settings

- `SAFEDB_WORKER_INTERVAL_SECONDS`: polling interval; defaults to `1` second and must be positive.
- `SAFEDB_WORKER_LEASE_SECONDS`: stale claim recovery lease; defaults to `600` seconds and must be positive.

Set the lease comfortably above the longest possible analysis or execution operation, including connection acquisition, planning, query execution, and result fetching. A lease that is too short can cause a second worker to reclaim an operation that is merely slow. Fencing tokens prevent the old worker from publishing stale state/results, but they do not prevent duplicate database work already in flight.

## Proposal state and recovery

The store requeues abandoned claims as follows:

| Abandoned state | Recovered state |
|---|---|
| `ANALYZING_WORKER` | `ANALYZING` |
| `EXECUTING` | `APPROVED` |

Lease recovery clears the old fencing token and writes an audit event atomically. A resumed worker with an old token cannot write completion state or publish a result. The current SQLite store is for single-host development only; do not scale it to multiple hosts.

## Health and endpoints

- `GET /health` is a basic API process liveness endpoint, not a database readiness probe.
- `GET /v1/datasources` lists only datasources allowed for the authenticated Agent.
- `GET /v1/review/queue` requires the reviewer token.
- `GET /v1/queries/{id}/result` is available only to the Agent that submitted a succeeded proposal.

The current health endpoint does not verify worker liveness, database reachability, queue age, or successful query execution. Add dedicated readiness, worker-heartbeat, queue-depth, error-rate and database connectivity monitoring before production use.

## Secret handling

- Use independent, high-entropy Agent and reviewer tokens.
- Use database accounts with least privilege and separate secret identities for each datasource.
- Do not put DB URLs into API/MCP environment variables or MCP client configuration.
- Prefer Secret Manager/Vault injection into the worker over plain environment files.
- Restrict worker network egress to the configured DB hosts and ports.
- Rotate datasource credentials and Agent/reviewer tokens immediately if a host, process, environment file, log or secret identity may have been exposed.

## Operational limitations

- SQLite is not a production queue or multi-host coordination service.
- Static bearer tokens are local-development authentication, not an enterprise identity system.
- The API currently persists SQL, parameters and results without application-layer encryption at rest.
- There is no built-in rate limiting, per-principal quota, queue-depth limit, metrics pipeline or immutable external audit sink.
- A single oversized field/LOB can be materialized by a driver before the response byte limit is applied.
- Unit tests do not replace live MySQL 8, PostgreSQL 16 and Oracle 19c integration tests.
