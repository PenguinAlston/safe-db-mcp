import os
from datetime import UTC, datetime, timedelta

import pytest

from safe_db_mcp.store_pg import PostgresProposalStore


@pytest.fixture
def store():
    url = os.getenv("SAFEDB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SAFEDB_TEST_POSTGRES_URL not configured")
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS audit_events"))
        conn.execute(text("DROP TABLE IF EXISTS proposals"))
    engine.dispose()
    yield PostgresProposalStore(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS audit_events"))
        conn.execute(text("DROP TABLE IF EXISTS proposals"))
    engine.dispose()


def audit_rows(store):
    with store.engine.connect() as conn:
        from sqlalchemy import text

        return conn.execute(
            text("SELECT proposal_id, event_type FROM audit_events ORDER BY event_id")
        ).all()


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


def test_create_and_get_roundtrip(store):
    current = datetime.now(UTC)
    store.create({
        "proposal_id": "p1", "datasource_id": "db", "requester_id": "agent",
        "purpose": "test", "sql": "SELECT id FROM public.items LIMIT 1",
        "parameters": {"limit": 1}, "sql_hash": "hash", "status": "ANALYZING",
        "created_at": current.isoformat(),
        "expires_at": (current + timedelta(minutes=15)).isoformat(),
    }, audit_event={
        "event_type": "query.submitted", "actor_id": "agent",
        "created_at": current.isoformat(), "details": {"sql_hash": "hash"},
    })
    stored = store.get("p1")
    assert stored["datasource_id"] == "db"
    assert stored["parameters"] == {"limit": 1}
    assert stored["sql"] == "SELECT id FROM public.items LIMIT 1"
    assert stored["risk"] is None and stored["approval"] is None
    assert datetime.fromisoformat(stored["expires_at"]) > current
    assert ("p1", "query.submitted") in audit_rows(store)
    assert store.get("missing") is None


def test_claim_returns_unique_token_and_audits(store):
    current = datetime.now(UTC)
    store.create({
        "proposal_id": "p1", "datasource_id": "db", "requester_id": "agent",
        "purpose": "test", "sql": "SELECT id FROM public.items LIMIT 1",
        "parameters": {}, "sql_hash": "hash", "status": "ANALYZING",
        "created_at": current.isoformat(),
        "expires_at": (current + timedelta(minutes=15)).isoformat(),
    })
    token = store.claim("p1", "ANALYZING", "ANALYZING_WORKER")
    assert token and len(token) == 32
    assert store.claim("p1", "ANALYZING", "ANALYZING_WORKER") is None
    assert store.claim("p1", "WRONG_STATUS", "ANALYZING_WORKER") is None
    claimed = store.get("p1")
    assert claimed["status"] == "ANALYZING_WORKER"
    assert claimed["claim_token"] == token
    assert store.owns_claim("p1", token, "ANALYZING_WORKER")
    assert not store.owns_claim("p1", "f" * 32, "ANALYZING_WORKER")
    assert not store.owns_claim("p1", token, "EXECUTING")
    assert ("p1", "query.analysis_started") in audit_rows(store)


def test_update_cas_rejects_wrong_expected_state_or_token(store):
    make_pending(store)
    assert not store.update(
        "proposal", expected_status="EXECUTING", error_code="x"
    )
    token = store.claim("proposal", "PENDING_APPROVAL", "EXECUTING")
    assert not store.update(
        "proposal", expected_status="EXECUTING", expected_claim_token="f" * 32,
        error_code="x",
    )
    assert store.update(
        "proposal", expected_status="EXECUTING", expected_claim_token=token,
        error_code="x",
    )
    with pytest.raises(ValueError, match="Unsupported"):
        store.update("proposal", review_token="attacker-controlled")


def test_pending_transition_rotates_token_and_clears_approval(store):
    old = make_pending(store)
    token = old.get("review_token")
    assert token and len(token) == 32
    assert store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(old))
    claim_token = store.claim("proposal", "APPROVED", "EXECUTING")
    assert store.update(
        "proposal", expected_status="EXECUTING", expected_claim_token=claim_token,
        status="PENDING_APPROVAL", approval_json=decision(old),
    )
    fresh = store.get("proposal")
    assert fresh["review_token"] != token
    assert fresh["approval"] is None
    assert fresh["claim_token"] is None
    assert not store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(old))
    assert store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(fresh))
    assert not store.decide("proposal", "PENDING_APPROVAL", "APPROVED", decision(fresh))


def test_decision_cas_checks_hash_token_and_expiry(store):
    proposal = make_pending(store, expired=True)
    approval = decision(proposal)
    approval["sql_hash"] = "different"
    assert not store.decide("proposal", "PENDING_APPROVAL", "APPROVED", approval)
    proposal = make_pending(store, "proposal-2")
    approval = decision(proposal)
    approval["review_token"] = "a" * 32
    assert not store.decide("proposal-2", "PENDING_APPROVAL", "APPROVED", approval)
    approval = decision(proposal)
    approval.pop("review_token")
    assert not store.decide("proposal-2", "PENDING_APPROVAL", "APPROVED", approval)
    assert audit_rows(store) == []
    assert store.get("proposal")["status"] == "PENDING_APPROVAL"
    assert store.get("proposal-2")["status"] == "PENDING_APPROVAL"


def test_expire_pending_approvals_is_atomic_and_idempotent(store):
    make_pending(store, "old-a", expired=True)
    make_pending(store, "old-b", expired=True)
    live = make_pending(store, "live")
    active = make_pending(store, "active")
    assert store.decide("active", "PENDING_APPROVAL", "APPROVED", decision(active))
    token = store.claim("active", "APPROVED", "EXECUTING")
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


def test_expire_pending_approvals_rolls_back_entire_batch_on_audit_failure(
    store, monkeypatch
):
    old_a = make_pending(store, "old-a", expired=True)
    old_b = make_pending(store, "old-b", expired=True)
    original = store._insert_audit
    calls = {"count": 0}

    def failing_audit(conn, proposal_id, event):
        calls["count"] += 1
        if proposal_id == "old-b":
            raise RuntimeError("forced audit failure")
        original(conn, proposal_id, event)

    monkeypatch.setattr(store, "_insert_audit", failing_audit)
    with pytest.raises(RuntimeError, match="forced audit failure"):
        store.expire_pending_approvals()
    assert store.get("old-a") == old_a
    assert store.get("old-b") == old_b
    assert audit_rows(store) == []


def test_recover_stale_claims_requeues_and_invalidates_tokens(store):
    current = datetime.now(UTC)
    for proposal_id, status in (("stale-a", "ANALYZING_WORKER"), ("stale-b", "EXECUTING")):
        store.create({
            "proposal_id": proposal_id, "datasource_id": "db", "requester_id": "agent",
            "purpose": "test", "sql": "SELECT id FROM public.items LIMIT 1",
            "parameters": {}, "sql_hash": "hash", "status": "ANALYZING",
            "created_at": current.isoformat(),
            "expires_at": (current + timedelta(minutes=15)).isoformat(),
        })
        store.update(proposal_id, expected_status="ANALYZING", status="PENDING_APPROVAL")
        if status == "EXECUTING":
            proposal = store.get(proposal_id)
            store.decide(proposal_id, "PENDING_APPROVAL", "APPROVED", decision(proposal))
        token = store.claim(
            proposal_id,
            "APPROVED" if status == "EXECUTING" else "PENDING_APPROVAL",
            status,
        )
        assert token
    assert store.recover_stale_claims(lease_seconds=600) == 0
    with store.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text(
            "UPDATE proposals SET claimed_at = now() - interval '1 hour'"
        ))
    assert store.recover_stale_claims(lease_seconds=600) == 2
    assert store.get("stale-a")["status"] == "ANALYZING"
    assert store.get("stale-b")["status"] == "APPROVED"
    for proposal_id in ("stale-a", "stale-b"):
        stored = store.get(proposal_id)
        assert stored["claim_token"] is None
        assert not store.owns_claim(proposal_id, "x", stored["status"])
    assert audit_rows(store).count(("stale-a", "worker.lease_recovered")) == 1
    with pytest.raises(ValueError, match="positive"):
        store.recover_stale_claims(lease_seconds=0)


def test_list_status_filters_expired_and_paginates(store):
    make_pending(store, "expired", expired=True)
    for proposal_id in ("c", "b", "a"):
        make_pending(store, proposal_id)
    with store.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text("UPDATE proposals SET created_at='2026-01-01T00:00:00+00:00'"))
    assert [p["proposal_id"] for p in store.list_status(
        "PENDING_APPROVAL", limit=1, offset=1
    )] == ["b"]
    assert [p["proposal_id"] for p in store.list_status("PENDING_APPROVAL")] == ["a", "b", "c"]
    assert [p["proposal_id"] for p in store.list_status(
        "PENDING_APPROVAL", limit=2, offset=2
    )] == ["c"]


def test_json_fields_roundtrip(store):
    make_pending(store)
    risk = {"score": 20, "level": "MEDIUM", "reasons": ["no limit"]}
    result = {"rows": [{"id": 1}], "returned_rows": 1, "truncated": False}
    assert store.update(
        "proposal",
        expected_status="PENDING_APPROVAL",
        risk_json=risk,
        result_json=result,
        error_code=None,
    )
    stored = store.get("proposal")
    assert stored["risk"] == risk
    assert stored["result"] == result
    assert stored["error_code"] is None
