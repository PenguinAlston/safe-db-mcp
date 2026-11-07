from __future__ import annotations

import sqlite3

import pytest

from safe_db_mcp.store import ProposalStore


@pytest.mark.parametrize(
    ("initial_status", "expected_status"),
    [("ANALYZING_WORKER", "ANALYZING"), ("EXECUTING", "APPROVED")],
)
def test_stale_worker_claim_is_recovered(tmp_path, initial_status, expected_status):
    store = ProposalStore(str(tmp_path / "store.sqlite3"))
    proposal = {
        "proposal_id": "proposal-1",
        "datasource_id": "db",
        "requester_id": "agent",
        "purpose": "test query",
        "sql": "SELECT id FROM public.items LIMIT 1",
        "parameters": {},
        "sql_hash": "hash",
        "status": "ANALYZING" if initial_status == "ANALYZING_WORKER" else "APPROVED",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    store.create(proposal)
    expected = "ANALYZING" if initial_status == "ANALYZING_WORKER" else "APPROVED"
    assert store.claim("proposal-1", expected, initial_status)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE proposals SET claimed_at=datetime('now', '-1 hour') WHERE proposal_id=?",
            ("proposal-1",),
        )

    assert store.recover_stale_claims(60) == 1
    assert store.get("proposal-1")["status"] == expected_status


def test_non_stale_claim_is_not_recovered(tmp_path):
    store = ProposalStore(str(tmp_path / "store.sqlite3"))
    proposal = {
        "proposal_id": "proposal-2",
        "datasource_id": "db",
        "requester_id": "agent",
        "purpose": "test query",
        "sql": "SELECT id FROM public.items LIMIT 1",
        "parameters": {},
        "sql_hash": "hash",
        "status": "ANALYZING",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    store.create(proposal)
    assert store.claim("proposal-2", "ANALYZING", "ANALYZING_WORKER")
    assert store.recover_stale_claims(600) == 0
    assert store.get("proposal-2")["status"] == "ANALYZING_WORKER"
