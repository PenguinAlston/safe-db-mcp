from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from contextlib import closing
from pathlib import Path
from typing import Any


class ProposalStore:
    """SQLite development store. Use PostgreSQL + row locks for multi-worker production."""

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("SAFEDB_DB_PATH", "./data/safedb.sqlite3")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _insert_audit(
        conn: sqlite3.Connection,
        proposal_id: str,
        event: dict[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO audit_events
               (proposal_id,event_type,actor_id,created_at,details_json)
               VALUES (?,?,?,?,?)""",
            (
                proposal_id,
                event["event_type"],
                event.get("actor_id", "worker"),
                event.get("created_at") or datetime.now(UTC).isoformat(),
                json.dumps(event.get("details") or {}, default=str),
            ),
        )

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                datasource_id TEXT NOT NULL,
                requester_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                sql_text TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                sql_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                risk_json TEXT,
                approval_json TEXT,
                result_json TEXT,
                error_code TEXT,
                claimed_at TEXT,
                claim_token TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_proposals_status
                ON proposals(status, created_at);
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()
            }
            if "claimed_at" not in columns:
                conn.execute("ALTER TABLE proposals ADD COLUMN claimed_at TEXT")
            if "claim_token" not in columns:
                conn.execute("ALTER TABLE proposals ADD COLUMN claim_token TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_proposals_claimed_at "
                "ON proposals(status, claimed_at)"
            )

    def create(
        self,
        proposal: dict[str, Any],
        audit_event: dict[str, Any] | None = None,
    ):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO proposals
                    (proposal_id,datasource_id,requester_id,purpose,sql_text,parameters_json,
                     sql_hash,status,created_at,expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        proposal["proposal_id"], proposal["datasource_id"],
                        proposal["requester_id"], proposal["purpose"], proposal["sql"],
                        json.dumps(proposal["parameters"], default=str),
                        proposal["sql_hash"], proposal["status"],
                        proposal["created_at"], proposal["expires_at"],
                    ),
                )
                if audit_event:
                    self._insert_audit(conn, proposal["proposal_id"], audit_event)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def list_status(self, status: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status=? ORDER BY created_at LIMIT ?",
                (status, limit),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def claim(self, proposal_id: str, expected: str, claimed: str) -> str | None:
        """Claim work and return a per-claim fencing token, or None on a lost race."""
        token = uuid.uuid4().hex
        event_type = (
            "query.analysis_started" if claimed == "ANALYZING_WORKER"
            else "query.execution_started"
        )
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """UPDATE proposals
                       SET status=?, claimed_at=CURRENT_TIMESTAMP, claim_token=?
                       WHERE proposal_id=? AND status=?""",
                    (claimed, token, proposal_id, expected),
                )
                if cur.rowcount == 1:
                    self._insert_audit(conn, proposal_id, {
                        "event_type": event_type,
                        "actor_id": "worker",
                        "details": {"claimed_status": claimed},
                    })
                    conn.commit()
                    return token
                conn.commit()
                return None
            except Exception:
                conn.rollback()
                raise

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
        if "status" in fields and fields["status"] not in {"ANALYZING_WORKER", "EXECUTING"}:
            fields["claimed_at"] = None
            fields["claim_token"] = None

        assignments = ", ".join(f"{key}=?" for key in fields)
        values = [
            json.dumps(value, default=str)
            if key.endswith("_json") and value is not None else value
            for key, value in fields.items()
        ]
        where = "proposal_id=?"
        values.append(proposal_id)
        if expected_claim_token is not None:
            where += " AND claim_token=?"
            values.append(expected_claim_token)
        if expected_status is not None:
            where += " AND status=?"
            values.append(expected_status)

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    f"UPDATE proposals SET {assignments} WHERE {where}", values
                )
                if cur.rowcount == 1 and audit_event:
                    self._insert_audit(conn, proposal_id, audit_event)
                conn.commit()
                return cur.rowcount == 1
            except Exception:
                conn.rollback()
                raise

    def owns_claim(
        self, proposal_id: str, claim_token: str | None, expected_status: str
    ) -> bool:
        if not claim_token:
            return False
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT 1 FROM proposals
                   WHERE proposal_id=? AND claim_token=? AND status=?""",
                (proposal_id, claim_token, expected_status),
            ).fetchone()
        return row is not None

    def recover_stale_claims(self, lease_seconds: int = 600) -> int:
        """Requeue abandoned claims, invalidate their fencing tokens, and audit atomically."""
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stale = conn.execute(
                    """SELECT proposal_id,status FROM proposals
                       WHERE status IN ('ANALYZING_WORKER', 'EXECUTING')
                         AND claimed_at < datetime('now', ?)""",
                    (f"-{lease_seconds} seconds",),
                ).fetchall()
                for row in stale:
                    self._insert_audit(conn, row["proposal_id"], {
                        "event_type": "worker.lease_recovered",
                        "actor_id": "worker",
                        "details": {"previous_status": row["status"]},
                    })
                cur = conn.execute(
                    """UPDATE proposals
                       SET status=CASE status
                           WHEN 'ANALYZING_WORKER' THEN 'ANALYZING'
                           WHEN 'EXECUTING' THEN 'APPROVED'
                       END,
                       claimed_at=NULL,
                       claim_token=NULL
                       WHERE status IN ('ANALYZING_WORKER', 'EXECUTING')
                         AND claimed_at < datetime('now', ?)""",
                    (f"-{lease_seconds} seconds",),
                )
                conn.commit()
                return cur.rowcount
            except Exception:
                conn.rollback()
                raise

    def decide(
        self,
        proposal_id: str,
        expected: str,
        status: str,
        approval: dict,
        audit_event: dict[str, Any] | None = None,
    ):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """UPDATE proposals SET status=?, approval_json=?
                       WHERE proposal_id=? AND status=?""",
                    (status, json.dumps(approval), proposal_id, expected),
                )
                if cur.rowcount == 1 and audit_event:
                    self._insert_audit(conn, proposal_id, audit_event)
                conn.commit()
                return cur.rowcount == 1
            except Exception:
                conn.rollback()
                raise

    def audit(
        self,
        proposal_id: str,
        event_type: str,
        actor_id: str,
        created_at: str,
        details: dict[str, Any] | None = None,
    ):
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO audit_events
                   (proposal_id,event_type,actor_id,created_at,details_json)
                   VALUES (?,?,?,?,?)""",
                (proposal_id, event_type, actor_id, created_at,
                 json.dumps(details or {}, default=str)),
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
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
        return item
