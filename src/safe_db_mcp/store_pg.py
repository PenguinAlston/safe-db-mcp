from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


class PostgresProposalStore:
    """PostgreSQL proposal/audit store. Must preserve the fencing/CAS semantics of
    store.ProposalStore: per-claim tokens, review-token rotation on every
    PENDING_APPROVAL transition, compare-and-swap status updates, and audit
    events committed in the same transaction as the state change."""

    _SCHEMA = (
        """CREATE TABLE IF NOT EXISTS proposals (
            proposal_id TEXT PRIMARY KEY,
            datasource_id TEXT NOT NULL,
            requester_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            sql_text TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            sql_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            risk_json TEXT,
            approval_json TEXT,
            result_json TEXT,
            error_code TEXT,
            claimed_at TIMESTAMPTZ,
            claim_token TEXT,
            review_token TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_claimed_at ON proposals(status, claimed_at)",
        """CREATE TABLE IF NOT EXISTS audit_events (
            event_id BIGSERIAL PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            details_json TEXT NOT NULL
        )""",
    )

    def __init__(self, url: str):
        self.engine: Engine = create_engine(url, pool_pre_ping=True)
        with self.engine.begin() as conn:
            for statement in self._SCHEMA:
                conn.execute(text(statement))

    @staticmethod
    def _insert_audit(
        conn: Connection, proposal_id: str, event: dict[str, Any]
    ) -> None:
        conn.execute(
            text(
                """INSERT INTO audit_events
                   (proposal_id, event_type, actor_id, created_at, details_json)
                   VALUES (:pid, :etype, :actor, :created, :details)"""
            ),
            {
                "pid": proposal_id,
                "etype": event["event_type"],
                "actor": event.get("actor_id", "worker"),
                "created": event.get("created_at") or datetime.now(UTC).isoformat(),
                "details": json.dumps(event.get("details") or {}, default=str),
            },
        )

    def create(
        self,
        proposal: dict[str, Any],
        audit_event: dict[str, Any] | None = None,
    ):
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """INSERT INTO proposals
                       (proposal_id, datasource_id, requester_id, purpose, sql_text,
                        parameters_json, sql_hash, status, created_at, expires_at)
                       VALUES (:pid, :ds, :req, :purpose, :sql, :params, :hash,
                               :status, :created, :expires)"""
                ),
                {
                    "pid": proposal["proposal_id"],
                    "ds": proposal["datasource_id"],
                    "req": proposal["requester_id"],
                    "purpose": proposal["purpose"],
                    "sql": proposal["sql"],
                    "params": json.dumps(proposal["parameters"], default=str),
                    "hash": proposal["sql_hash"],
                    "status": proposal["status"],
                    "created": proposal["created_at"],
                    "expires": proposal["expires_at"],
                },
            )
            if audit_event:
                self._insert_audit(conn, proposal["proposal_id"], audit_event)

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM proposals WHERE proposal_id=:pid"),
                {"pid": proposal_id},
            ).mappings().first()
        return self._decode(row)

    def list_status(
        self, status: str, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        # Filter at read time as well as sweeping: a review can expire between the two.
        expiry_filter = " AND expires_at > now()" if status == "PENDING_APPROVAL" else ""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT * FROM proposals WHERE status=:status{expiry_filter} "
                    "ORDER BY created_at, proposal_id LIMIT :limit OFFSET :offset"
                ),
                {"status": status, "limit": limit, "offset": offset},
            ).mappings().all()
        return [self._decode(row) for row in rows]

    def expire_pending_approvals(self) -> int:
        with self.engine.begin() as conn:
            expired = conn.execute(
                text(
                    """UPDATE proposals
                       SET status='EXPIRED', error_code='PROPOSAL_EXPIRED'
                       WHERE status='PENDING_APPROVAL' AND expires_at <= now()
                       RETURNING proposal_id"""
                )
            ).mappings().all()
            for row in expired:
                self._insert_audit(conn, row["proposal_id"], {
                    "event_type": "query.expired",
                    "actor_id": "worker",
                })
            return len(expired)

    def claim(self, proposal_id: str, expected: str, claimed: str) -> str | None:
        token = uuid.uuid4().hex
        event_type = (
            "query.analysis_started" if claimed == "ANALYZING_WORKER"
            else "query.execution_started"
        )
        with self.engine.begin() as conn:
            cur = conn.execute(
                text(
                    """UPDATE proposals
                       SET status=:claimed, claimed_at=now(), claim_token=:token
                       WHERE proposal_id=:pid AND status=:expected
                       RETURNING proposal_id"""
                ),
                {"claimed": claimed, "token": token, "pid": proposal_id,
                 "expected": expected},
            )
            if cur.rowcount == 1:
                self._insert_audit(conn, proposal_id, {
                    "event_type": event_type,
                    "actor_id": "worker",
                    "details": {"claimed_status": claimed},
                })
                return token
            return None

    def update(
        self,
        proposal_id: str,
        *,
        expected_claim_token: str | None = None,
        expected_status: str | None = None,
        audit_event: dict[str, Any] | None = None,
        **fields,
    ) -> bool:
        allowed = {"status", "risk_json", "approval_json", "result_json", "error_code"}
        if set(fields) - allowed:
            raise ValueError("Unsupported proposal update field")
        if not fields:
            return False
        if fields.get("status") == "PENDING_APPROVAL":
            # Only the store can mint a review round; invalidate any previous decision.
            fields["review_token"] = uuid.uuid4().hex
            fields["approval_json"] = None
        if "status" in fields and fields["status"] not in {"ANALYZING_WORKER", "EXECUTING"}:
            fields["claimed_at"] = None
            fields["claim_token"] = None

        assignments = ", ".join(f"{key}=:{key}" for key in fields)
        values = {
            key: (
                json.dumps(value, default=str)
                if key.endswith("_json") and value is not None else value
            )
            for key, value in fields.items()
        }
        where = "proposal_id=:pid"
        values["pid"] = proposal_id
        if expected_claim_token is not None:
            where += " AND claim_token=:expected_claim_token"
            values["expected_claim_token"] = expected_claim_token
        if expected_status is not None:
            where += " AND status=:expected_status"
            values["expected_status"] = expected_status

        with self.engine.begin() as conn:
            cur = conn.execute(
                text(f"UPDATE proposals SET {assignments} WHERE {where}"), values
            )
            if cur.rowcount == 1 and audit_event:
                self._insert_audit(conn, proposal_id, audit_event)
            return cur.rowcount == 1

    def owns_claim(
        self, proposal_id: str, claim_token: str | None, expected_status: str
    ) -> bool:
        if not claim_token:
            return False
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    """SELECT 1 FROM proposals
                       WHERE proposal_id=:pid AND claim_token=:token AND status=:status"""
                ),
                {"pid": proposal_id, "token": claim_token, "status": expected_status},
            ).first()
        return row is not None

    def recover_stale_claims(self, lease_seconds: int = 600) -> int:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self.engine.begin() as conn:
            stale = conn.execute(
                text(
                    """SELECT proposal_id, status FROM proposals
                       WHERE status IN ('ANALYZING_WORKER', 'EXECUTING')
                         AND claimed_at < now() - make_interval(secs => :lease)
                       FOR UPDATE"""
                ),
                {"lease": lease_seconds},
            ).mappings().all()
            for row in stale:
                self._insert_audit(conn, row["proposal_id"], {
                    "event_type": "worker.lease_recovered",
                    "actor_id": "worker",
                    "details": {"previous_status": row["status"]},
                })
            cur = conn.execute(
                text(
                    """UPDATE proposals
                       SET status=CASE status
                           WHEN 'ANALYZING_WORKER' THEN 'ANALYZING'
                           WHEN 'EXECUTING' THEN 'APPROVED'
                       END,
                       claimed_at=NULL,
                       claim_token=NULL
                       WHERE status IN ('ANALYZING_WORKER', 'EXECUTING')
                         AND claimed_at < now() - make_interval(secs => :lease)"""
                ),
                {"lease": lease_seconds},
            )
            return cur.rowcount

    def decide(
        self,
        proposal_id: str,
        expected: str,
        status: str,
        approval: dict,
        audit_event: dict[str, Any] | None = None,
    ):
        with self.engine.begin() as conn:
            cur = conn.execute(
                text(
                    """UPDATE proposals SET status=:status, approval_json=:approval
                       WHERE proposal_id=:pid AND status=:expected
                         AND review_token=:rt AND review_token <> '' AND sql_hash=:sh
                         AND expires_at > now()"""
                ),
                {"status": status, "approval": json.dumps(approval),
                 "pid": proposal_id, "expected": expected,
                 "rt": approval.get("review_token"), "sh": approval.get("sql_hash")},
            )
            if cur.rowcount == 1 and audit_event:
                self._insert_audit(conn, proposal_id, audit_event)
            return cur.rowcount == 1

    def audit(
        self,
        proposal_id: str,
        event_type: str,
        actor_id: str,
        created_at: str,
        details: dict[str, Any] | None = None,
    ):
        with self.engine.begin() as conn:
            self._insert_audit(conn, proposal_id, {
                "event_type": event_type,
                "actor_id": actor_id,
                "created_at": created_at,
                "details": details or {},
            })

    @staticmethod
    def _decode(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["parameters"] = json.loads(item.pop("parameters_json"))
        for source, target in (
            ("risk_json", "risk"),
            ("approval_json", "approval"),
            ("result_json", "result"),
        ):
            value = item.pop(source)
            item[target] = json.loads(value) if value else None
        item["sql"] = item.pop("sql_text")
        for key in ("created_at", "expires_at", "claimed_at"):
            if isinstance(item.get(key), datetime):
                item[key] = item[key].isoformat()
        return item
