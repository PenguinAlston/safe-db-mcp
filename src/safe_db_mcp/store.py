from __future__ import annotations

import json
import os
import sqlite3
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
                error_code TEXT
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

    def create(self, proposal: dict[str, Any]):
        with closing(self._connect()) as conn:
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

    def claim(self, proposal_id: str, expected: str, claimed: str) -> bool:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE proposals SET status=? WHERE proposal_id=? AND status=?",
                (claimed, proposal_id, expected),
            )
            conn.commit()
            return cur.rowcount == 1

    def update(self, proposal_id: str, **fields):
        allowed = {"status", "risk_json", "approval_json", "result_json", "error_code"}
        if set(fields) - allowed:
            raise ValueError("Unsupported proposal update field")
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = []
        for key, value in fields.items():
            values.append(
                json.dumps(value, default=str)
                if key.endswith("_json") and value is not None else value
            )
        values.append(proposal_id)
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE proposals SET {assignments} WHERE proposal_id=?", values
            )

    def decide(self, proposal_id: str, expected: str, status: str, approval: dict):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """UPDATE proposals SET status=?, approval_json=?
                   WHERE proposal_id=? AND status=?""",
                (status, json.dumps(approval), proposal_id, expected),
            )
            conn.commit()
            return cur.rowcount == 1

    def audit(self, proposal_id: str, event_type: str, actor_id: str,
              created_at: str, details: dict[str, Any] | None = None):
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
