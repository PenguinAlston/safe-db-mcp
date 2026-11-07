from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

import pytest

from safe_db_mcp.config import DataSource
from safe_db_mcp.security.risk import QueryPlan
from safe_db_mcp.service import QueryService
from safe_db_mcp.store import ProposalStore
from safe_db_mcp.worker import run_once


@pytest.fixture
def setup_service(tmp_path, monkeypatch):
    store = ProposalStore(str(tmp_path / "service.sqlite3"))
    datasource = DataSource(
        "db", "test", "postgres", "UNUSED_TEST_SECRET", "postgres_transaction",
        frozenset({"public.items"}), frozenset(), True,
        1000, 10000, 100, 100000, 1000, frozenset({"agent"}),
    )
    service = QueryService(store, {"db": datasource})
    adapter = Mock()
    adapter.verify_readonly.return_value = True
    adapter.verify_allowed_tables.return_value = True
    adapter.explain.return_value = QueryPlan(100, 1, {})
    adapter.execute_readonly.return_value = {
        "rows": [], "returned_rows": 0, "result_bytes": 0,
        "duration_ms": 1, "scanned_rows": None, "truncated": False,
    }
    factory = Mock(return_value=adapter)
    monkeypatch.setattr("safe_db_mcp.adapters.factory.create_adapter", factory)
    return service, store, adapter, factory


def submit(service):
    return service.prepare(
        "db", "SELECT id FROM public.items LIMIT 1", "test query", {}, "agent"
    )["proposal_id"]


def claim(store, proposal_id, execution=False):
    expected, active = ("APPROVED", "EXECUTING") if execution else (
        "ANALYZING", "ANALYZING_WORKER"
    )
    assert store.claim(proposal_id, expected, active)
    return store.get(proposal_id)


def pending(service, store):
    service.datasources["db"] = replace(service.datasources["db"], auto_execute=False)
    proposal_id = submit(service)
    service.process_analysis(claim(store, proposal_id))
    return store.get(proposal_id)


def approve(store, proposal):
    # Keep setup independent of the HTTP/service decision interface under test.
    assert store.decide(
        proposal["proposal_id"], "PENDING_APPROVAL", "APPROVED",
        {"decision": "APPROVED", "sql_hash": proposal["sql_hash"],
         "review_token": proposal.get("review_token")},
    )


def events(store, proposal_id):
    with sqlite3.connect(store.path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT event_type FROM audit_events WHERE proposal_id=? ORDER BY event_id",
            (proposal_id,),
        )]


@pytest.mark.parametrize("execution", [False, True], ids=["analysis", "execution"])
@pytest.mark.parametrize("change", ["revoke", "remove"])
def test_current_datasource_authorization_precedes_adapter(setup_service, execution, change):
    service, store, adapter, factory = setup_service
    if execution:
        proposal = pending(service, store)
        approve(store, proposal)
        proposal_id = proposal["proposal_id"]
    else:
        proposal_id = submit(service)
    adapter.reset_mock()
    factory.reset_mock()
    if change == "revoke":
        service.datasources["db"] = replace(service.datasources["db"], allowed_agents=frozenset())
    else:
        del service.datasources["db"]

    assert run_once(store, service) == 1

    stored = store.get(proposal_id)
    assert stored["status"] == "BLOCKED"
    assert stored["error_code"] == (
        "AGENT_DATASOURCE_PERMISSION_CHANGED" if change == "revoke"
        else "DATASOURCE_NOT_REGISTERED"
    )
    factory.assert_not_called()
    adapter.execute_readonly.assert_not_called()
    assert events(store, proposal_id)[-1] == "query.blocked"


@pytest.mark.parametrize("execution", [False, True], ids=["auto", "manual"])
def test_planning_crosses_expiry_without_issuing_sql(setup_service, monkeypatch, execution):
    service, store, adapter, _ = setup_service
    if execution:
        proposal = pending(service, store)
        approve(store, proposal)
        proposal_id = proposal["proposal_id"]
    else:
        proposal_id = submit(service)
    proposal = claim(store, proposal_id, execution)
    expired = datetime.fromisoformat(proposal["expires_at"])

    def explain(*args):
        monkeypatch.setattr("safe_db_mcp.service.now", lambda: expired)
        return QueryPlan(100, 1, {})

    adapter.explain.side_effect = explain
    process = service.process_execution if execution else service.process_analysis
    process(proposal)

    stored = store.get(proposal_id)
    assert stored["status"] == "EXPIRED"
    assert stored["error_code"] == "PROPOSAL_EXPIRED"
    assert stored["result"] is None
    adapter.execute_readonly.assert_not_called()
    assert events(store, proposal_id)[-1] == "query.expired"
    adapter.close.assert_called()


@pytest.mark.parametrize("execution", [False, True], ids=["auto", "manual"])
def test_authorized_unexpired_query_succeeds(setup_service, execution):
    service, store, adapter, _ = setup_service
    if execution:
        proposal = pending(service, store)
        approve(store, proposal)
        proposal_id = proposal["proposal_id"]
    else:
        proposal_id = submit(service)
    assert run_once(store, service) == 1
    assert store.get(proposal_id)["status"] == "SUCCEEDED"
    adapter.execute_readonly.assert_called_once()
    assert events(store, proposal_id)[-1] == "query.succeeded"


def test_hard_scan_limit_replan_blocks_instead_of_requesting_review(setup_service):
    service, store, adapter, _ = setup_service
    proposal = pending(service, store)
    approve(store, proposal)
    adapter.explain.return_value = QueryPlan(10001, 1, {})
    service.process_execution(claim(store, proposal["proposal_id"], execution=True))
    stored = store.get(proposal["proposal_id"])
    assert stored["status"] == "BLOCKED"
    assert stored["error_code"] == "HARD_SCAN_LIMIT_EXCEEDED"
    adapter.execute_readonly.assert_not_called()
    assert "query.approval_invalidated" not in events(store, proposal["proposal_id"])


def test_old_loaded_snapshot_cannot_approve_new_review_round(setup_service):
    service, store, adapter, _ = setup_service
    old = pending(service, store)
    old_token = service.public_view(old).get("review_token")
    assert old_token and re.fullmatch(r"[0-9a-f]{32}", old_token)
    service.decide(old, True, "reviewer", "approved first review", old_token)
    adapter.explain.return_value = QueryPlan(200, 1, {})
    service.process_execution(claim(store, old["proposal_id"], execution=True))
    fresh = store.get(old["proposal_id"])
    assert fresh["status"] == "PENDING_APPROVAL"
    assert fresh["approval"] is None
    assert fresh["review_token"] != old_token
    before = events(store, old["proposal_id"])

    # The caller's snapshot still says PENDING and its token matches that old snapshot.
    with pytest.raises(ValueError, match="state changed"):
        service.decide(old, True, "reviewer", "stale review", old_token)
    with pytest.raises(ValueError, match="review token"):
        service.decide(fresh, True, "reviewer", "stale body", old_token)
    assert events(store, old["proposal_id"]) == before
    assert store.get(old["proposal_id"])["status"] == "PENDING_APPROVAL"

    result = service.decide(fresh, True, "reviewer", "fresh review", fresh["review_token"])
    assert result["status"] == "APPROVED"
    assert result["approval"]["review_token"] == fresh["review_token"]
    with pytest.raises(ValueError):
        service.decide(fresh, True, "reviewer", "duplicate review", fresh["review_token"])
    assert events(store, old["proposal_id"]).count("query.approval_decision") == 2
    service.process_execution(claim(store, old["proposal_id"], execution=True))
    assert store.get(old["proposal_id"])["status"] == "SUCCEEDED"
    adapter.execute_readonly.assert_called_once()


@pytest.mark.parametrize("binding", ["missing", "empty", "mismatch", "missing_proposal"])
def test_execution_rejects_unbound_approval(setup_service, binding):
    service, store, adapter, factory = setup_service
    proposal = pending(service, store)
    approve(store, proposal)
    proposal = claim(store, proposal["proposal_id"], execution=True)
    # Simulate a legacy/corrupt stored approval without changing the valid SQL hash.
    if binding == "missing":
        proposal["approval"].pop("review_token", None)
    elif binding == "empty":
        proposal["approval"]["review_token"] = ""
        proposal["review_token"] = ""
    elif binding == "mismatch":
        proposal["approval"]["review_token"] = "f" * 32
    else:
        proposal.pop("review_token", None)
    factory.reset_mock()
    service.process_execution(proposal)
    stored = store.get(proposal["proposal_id"])
    assert stored["status"] == "BLOCKED"
    assert stored["error_code"] == "APPROVAL_BINDING_INVALID"
    factory.assert_not_called()
    adapter.execute_readonly.assert_not_called()


def test_decision_expiry_is_checked_at_database_commit(setup_service, monkeypatch):
    service, store, _, _ = setup_service
    proposal = pending(service, store)
    token = proposal.get("review_token")
    assert token
    original = store.decide

    def expire_then_decide(*args, **kwargs):
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE proposals SET expires_at=datetime('now', '-1 second')")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "decide", expire_then_decide)
    with pytest.raises(ValueError, match="state changed"):
        service.decide(proposal, True, "reviewer", "approve", token)
    assert store.get(proposal["proposal_id"])["approval"] is None
    assert "query.approval_decision" not in events(store, proposal["proposal_id"])


def test_worker_expires_pending_approvals_without_touching_active_claims(setup_service):
    service, store, adapter, _ = setup_service
    proposal = pending(service, store)
    active_id = submit(service)
    active = claim(store, active_id)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE proposals SET expires_at=datetime('now', '-1 second')")
    assert run_once(store, service) == 0
    assert store.get(proposal["proposal_id"])["status"] == "EXPIRED"
    assert events(store, proposal["proposal_id"])[-1] == "query.expired"
    assert store.owns_claim(active_id, active["claim_token"], "ANALYZING_WORKER")
    adapter.execute_readonly.assert_not_called()
