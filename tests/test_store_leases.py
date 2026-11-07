from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta

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


def make_pending(store, proposal_id="proposal", *, expired=False):
    current = datetime.now(UTC)
    store.create({
        "proposal_id": proposal_id, "datasource_id": "db", "requester_id": "agent",
        "purpose": "test review", "sql": "SELECT id FROM public.items LIMIT 1",
        "parameters": {}, "sql_hash": "hash", "status": "ANALYZING",
        "created_at": current.isoformat(),
        "expires_at": (current + timedelta(minutes=-1 if expired else 15)).isoformat(),
    })
    assert store.update(proposal_id, expected_status="ANALYZING", status="PENDING_APPROVAL")
    return store.get(proposal_id)


def decision(proposal):
    return {
        "decision": "APPROVED", "sql_hash": proposal["sql_hash"],
        "review_token": proposal.get("review_token"),
    }


def audit_rows(store):
    with sqlite3.connect(store.path) as conn:
        return conn.execute(
            "SELECT proposal_id,event_type FROM audit_events ORDER BY event_id"
        ).fetchall()


def test_pending_transition_rotates_token_and_clears_approval(tmp_path):
    store = ProposalStore(str(tmp_path / "review.sqlite3"))
    old = make_pending(store)
    token = old.get("review_token")
    assert token and re.fullmatch(r"[0-9a-f]{32}", token)
    assert store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(old))
    claim_token = store.claim("proposal", "APPROVED", "EXECUTING")
    assert store.update(
        "proposal", expected_status="EXECUTING", expected_claim_token=claim_token,
        status="PENDING_APPROVAL", approval_json=decision(old),
    )
    fresh = store.get("proposal")
    assert fresh["review_token"] != token
    assert re.fullmatch(r"[0-9a-f]{32}", fresh["review_token"])
    assert fresh["approval"] is None
    assert fresh["claim_token"] is None
    assert not store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(old))
    assert store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(fresh))
    assert not store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(fresh))


def test_loaded_decision_cannot_cross_pending_approved_pending_aba(tmp_path):
    store = ProposalStore(str(tmp_path / "aba.sqlite3"))
    old = make_pending(store)
    old_decision = decision(old)
    assert store.decide("proposal", "PENDING_APPROVAL", "APPROVED", old_decision)
    token = store.claim("proposal", "APPROVED", "EXECUTING")
    assert store.update(
        "proposal", expected_status="EXECUTING", expected_claim_token=token,
        status="PENDING_APPROVAL",
    )
    assert not store.decide(
        "proposal", "PENDING_APPROVAL", "APPROVED", old_decision,
        audit_event={"event_type": "query.approval_decision", "actor_id": "reviewer"},
    )
    assert ("proposal", "query.approval_decision") not in audit_rows(store)


def test_review_token_cannot_be_caller_assigned(tmp_path):
    store = ProposalStore(str(tmp_path / "review.sqlite3"))
    old = make_pending(store)
    with pytest.raises(ValueError, match="Unsupported"):
        store.update("proposal", status="PENDING_APPROVAL", review_token="a" * 32)
    assert store.get("proposal") == old


@pytest.mark.parametrize("invalid", ["hash", "token", "missing_token", "expired"])
def test_decision_cas_checks_hash_token_and_expiry(tmp_path, invalid):
    store = ProposalStore(str(tmp_path / "review.sqlite3"))
    proposal = make_pending(store, expired=(invalid == "expired"))
    approval = decision(proposal)
    if invalid == "hash":
        approval["sql_hash"] = "different hash"
    elif invalid == "token":
        approval["review_token"] = "a" * 32
    elif invalid == "missing_token":
        approval.pop("review_token")
    assert not store.decide(
        "proposal", "PENDING_APPROVAL", "APPROVED", approval,
        audit_event={"event_type": "query.approval_decision", "actor_id": "reviewer"},
    )
    assert store.get("proposal")["status"] == "PENDING_APPROVAL"
    assert store.get("proposal")["approval"] is None
    assert audit_rows(store) == []


def test_decision_and_token_rotation_roll_back_with_audit_failure(tmp_path):
    store = ProposalStore(str(tmp_path / "review.sqlite3"))
    old = make_pending(store)
    assert old.get("review_token")
    with sqlite3.connect(store.path) as conn:
        conn.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events
                        BEGIN SELECT RAISE(ABORT, 'test audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test audit failure"):
        store.decide(
            "proposal", "PENDING_APPROVAL", "APPROVED", decision(old),
            audit_event={"event_type": "query.approval_decision", "actor_id": "reviewer"},
        )
    assert store.get("proposal") == old
    assert audit_rows(store) == []
    with pytest.raises(sqlite3.IntegrityError, match="test audit failure"):
        store.update(
            "proposal", expected_status="PENDING_APPROVAL", status="PENDING_APPROVAL",
            audit_event={"event_type": "query.pending_approval"},
        )
    assert store.get("proposal") == old
    assert audit_rows(store) == []


def test_expire_pending_approvals_is_atomic_and_idempotent(tmp_path):
    store = ProposalStore(str(tmp_path / "expiry.sqlite3"))
    make_pending(store, "old-a", expired=True)
    make_pending(store, "old-b", expired=True)
    live = make_pending(store, "live")
    active = make_pending(store, "active")
    assert store.decide("active", "PENDING_APPROVAL", "APPROVED", decision(active))
    token = store.claim("active", "APPROVED", "EXECUTING")
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE proposals SET expires_at=datetime('now', '-1 second') "
            "WHERE proposal_id='active'"
        )
    assert store.expire_pending_approvals() == 2
    assert store.expire_pending_approvals() == 0
    for proposal_id in ("old-a", "old-b"):
        stored = store.get(proposal_id)
        assert stored["status"] == "EXPIRED"
        assert stored["error_code"] == "PROPOSAL_EXPIRED"
        assert audit_rows(store).count((proposal_id, "query.expired")) == 1
    assert store.get("live") == live
    assert store.owns_claim("active", token, "EXECUTING")
    assert ("active", "query.expired") not in audit_rows(store)


def test_expire_pending_approvals_rolls_back_entire_batch_on_audit_failure(tmp_path):
    store = ProposalStore(str(tmp_path / "expiry.sqlite3"))
    old_a = make_pending(store, "old-a", expired=True)
    old_b = make_pending(store, "old-b", expired=True)
    with sqlite3.connect(store.path) as conn:
        conn.execute("""CREATE TRIGGER fail_expiry_audit BEFORE INSERT ON audit_events
                        WHEN NEW.proposal_id='old-b'
                        BEGIN SELECT RAISE(ABORT, 'test audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test audit failure"):
        store.expire_pending_approvals()
    assert store.get("old-a") == old_a
    assert store.get("old-b") == old_b
    assert audit_rows(store) == []


def test_pending_list_filters_expiry_before_pagination_and_orders_ties(tmp_path):
    store = ProposalStore(str(tmp_path / "pages.sqlite3"))
    make_pending(store, "expired", expired=True)
    for proposal_id in ("c", "b", "a"):
        make_pending(store, proposal_id)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE proposals SET created_at='2026-01-01T00:00:00+00:00'")
    assert [p["proposal_id"] for p in store.list_status(
        "PENDING_APPROVAL", limit=1, offset=1
    )] == ["b"]
    assert [p["proposal_id"] for p in store.list_status("PENDING_APPROVAL")] == ["a", "b", "c"]


def test_legacy_pending_rows_get_persistent_tokens_but_approved_stays_unbound(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE proposals (
            proposal_id TEXT PRIMARY KEY, datasource_id TEXT NOT NULL,
            requester_id TEXT NOT NULL, purpose TEXT NOT NULL, sql_text TEXT NOT NULL,
            parameters_json TEXT NOT NULL, sql_hash TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, risk_json TEXT,
            approval_json TEXT, result_json TEXT, error_code TEXT
        )""")
        for proposal_id, status in (("pending-a", "PENDING_APPROVAL"),
                                    ("pending-b", "PENDING_APPROVAL"),
                                    ("approved", "APPROVED")):
            conn.execute(
                """INSERT INTO proposals VALUES
                   (?, 'db', 'agent', 'legacy test', 'SELECT id FROM public.items LIMIT 1',
                    '{}', 'hash', ?, ?, ?, NULL,
                    '{"decision":"APPROVED","sql_hash":"hash"}', NULL, NULL)""",
                (proposal_id, status, datetime.now(UTC).isoformat(),
                 (datetime.now(UTC) + timedelta(minutes=15)).isoformat()),
            )
    store = ProposalStore(path)
    tokens = [store.get(p).get("review_token") for p in ("pending-a", "pending-b")]
    assert all(token and re.fullmatch(r"[0-9a-f]{32}", token) for token in tokens)
    assert tokens[0] != tokens[1]
    assert store.get("pending-a")["approval"] is None
    assert store.get("approved")["review_token"] is None
    assert "review_token" not in store.get("approved")["approval"]
    reopened = ProposalStore(path)
    assert [reopened.get(p)["review_token"] for p in ("pending-a", "pending-b")] == tokens
