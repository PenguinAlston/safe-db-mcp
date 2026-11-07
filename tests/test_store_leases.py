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


def test_stale_worker_fencing_token_cannot_publish_after_approval_invalidation(tmp_path):
    store = ProposalStore(str(tmp_path / "fenced.sqlite3"))
    proposal = {
        "proposal_id": "proposal-fenced",
        "datasource_id": "db",
        "requester_id": "agent",
        "purpose": "test fencing",
        "sql": "SELECT id FROM public.items LIMIT 1",
        "parameters": {},
        "sql_hash": "hash",
        "status": "APPROVED",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    store.create(proposal)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE proposals SET approval_json=? WHERE proposal_id=?",
            ('{"decision":"APPROVED","sql_hash":"hash"}', "proposal-fenced"),
        )

    token_a = store.claim("proposal-fenced", "APPROVED", "EXECUTING")
    assert token_a
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE proposals SET claimed_at=datetime('now', '-1 hour') "
            "WHERE proposal_id=?",
            ("proposal-fenced",),
        )

    assert store.recover_stale_claims(60) == 1
    token_b = store.claim("proposal-fenced", "APPROVED", "EXECUTING")
    assert token_b and token_b != token_a

    # Worker B detects a changed risk profile and invalidates the old approval.
    assert store.update(
        "proposal-fenced",
        expected_claim_token=token_b,
        expected_status="EXECUTING",
        status="PENDING_APPROVAL",
        approval_json=None,
        risk_json={"score": 50, "level": "HIGH"},
    )

    # Worker A resumes with its old token and must not publish a stale result.
    assert not store.update(
        "proposal-fenced",
        expected_claim_token=token_a,
        expected_status="EXECUTING",
        status="SUCCEEDED",
        result_json={"rows": [{"id": 1}]},
    )
    final = store.get("proposal-fenced")
    assert final["status"] == "PENDING_APPROVAL"
    assert final["approval"] is None
    assert final["result"] is None


def test_claim_and_lease_recovery_are_audited(tmp_path):
    store = ProposalStore(str(tmp_path / "audit.sqlite3"))
    proposal = {
        "proposal_id": "proposal-audit",
        "datasource_id": "db",
        "requester_id": "agent",
        "purpose": "audit",
        "sql": "SELECT id FROM public.items LIMIT 1",
        "parameters": {},
        "sql_hash": "hash",
        "status": "ANALYZING",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    store.create(proposal, audit_event={
        "event_type": "query.submitted",
        "actor_id": "agent",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    token = store.claim("proposal-audit", "ANALYZING", "ANALYZING_WORKER")
    assert token
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE proposals SET claimed_at=datetime('now', '-1 hour') "
            "WHERE proposal_id=?",
            ("proposal-audit",),
        )
    assert store.recover_stale_claims(60) == 1

    with sqlite3.connect(store.path) as conn:
        events = [
            row[0] for row in conn.execute(
                "SELECT event_type FROM audit_events WHERE proposal_id=?",
                ("proposal-audit",),
            ).fetchall()
        ]
    assert "query.submitted" in events
    assert "query.analysis_started" in events
    assert "worker.lease_recovered" in events
