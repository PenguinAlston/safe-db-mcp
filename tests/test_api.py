from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from safe_db_mcp.config import DataSource
from safe_db_mcp.service import QueryService
from safe_db_mcp.store import ProposalStore


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    # api.py initializes its store at import time: point it at a temporary store first.
    monkeypatch.setenv("SAFEDB_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("SAFEDB_REVIEWER_TOKEN", "test-reviewer-placeholder")
    monkeypatch.setenv("SAFEDB_REVIEWER_ID", "reviewer")
    monkeypatch.setenv("SAFEDB_AGENT_TOKEN", "test-agent-placeholder")
    monkeypatch.setenv("SAFEDB_AGENT_ID", "agent")
    datasource = DataSource(
        "db", "test", "postgres", "UNUSED_TEST_SECRET", "postgres_transaction",
        frozenset({"public.items"}), frozenset(), False,
        1000, 10000, 100, 100000, 1000, frozenset({"agent"}),
    )
    datasources = {"db": datasource}
    monkeypatch.setattr("safe_db_mcp.config.load_datasources", lambda: datasources)
    api = importlib.import_module("safe_db_mcp.api")
    store = ProposalStore(str(tmp_path / "api.sqlite3"))
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "datasources", datasources)
    monkeypatch.setattr(api, "service", QueryService(store, datasources))
    with TestClient(api.app, headers={
        "Authorization": "Bearer test-reviewer-placeholder"
    }) as client:
        yield client, api


def make_pending(api, *, expired=False):
    proposal_id = api.service.prepare(
        "db", "SELECT id FROM public.items LIMIT 1", "review test", {}, "agent"
    )["proposal_id"]
    assert api.store.update(proposal_id, expected_status="ANALYZING", status="PENDING_APPROVAL")
    if expired:
        with sqlite3.connect(api.store.path) as conn:
            conn.execute(
                "UPDATE proposals SET expires_at=datetime('now', '-1 second'), "
                "created_at='2000-01-01T00:00:00+00:00' WHERE proposal_id=?",
                (proposal_id,),
            )
    return proposal_id


@pytest.mark.parametrize("token", [None, "", "a" * 31, "A" * 32, "z" * 32, 123, "a" * 32 + "\n"])
def test_decision_requires_well_formed_review_token(api_client, token):
    client, api = api_client
    proposal_id = make_pending(api)
    body = {"approve": True, "reason": "reviewed"}
    if token is not None:
        body["review_token"] = token
    response = client.post(f"/v1/queries/{proposal_id}/decision", json=body)
    assert response.status_code == 422
    assert api.store.get(proposal_id)["status"] == "PENDING_APPROVAL"


def test_http_stale_body_token_rejected_and_fresh_token_accepted_once(api_client):
    client, api = api_client
    proposal_id = make_pending(api)
    old = client.get("/v1/review/queue").json()["items"][0]
    old_token = old.get("review_token")
    assert old_token
    body = {"approve": True, "reason": "reviewed", "review_token": old_token}
    path = f"/v1/queries/{proposal_id}/decision"
    assert client.post(path, json=body).status_code == 200
    token = api.store.claim(proposal_id, "APPROVED", "EXECUTING")
    assert api.store.update(
        proposal_id, expected_status="EXECUTING", expected_claim_token=token,
        status="PENDING_APPROVAL", risk_json={"score": 20, "level": "MEDIUM"},
    )
    assert client.post(path, json=body).status_code == 409
    fresh = client.get("/v1/review/queue").json()["items"][0]
    assert fresh["review_token"] != old_token
    body["review_token"] = fresh["review_token"]
    result = client.post(path, json=body)
    assert result.status_code == 200
    assert result.json()["approval"]["review_token"] == fresh["review_token"]
    assert client.post(path, json=body).status_code == 409
    with sqlite3.connect(api.store.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='query.approval_decision'"
        ).fetchone()[0] == 2


def test_review_queue_expires_full_page_before_listing_live_item(api_client):
    client, api = api_client
    expired = [make_pending(api, expired=True) for _ in range(100)]
    live = make_pending(api)
    response = client.get("/v1/review/queue")
    assert response.status_code == 200
    assert [p["proposal_id"] for p in response.json()["items"]] == [live]
    assert response.json()["items"][0]["review_token"]
    assert all(api.store.get(proposal_id)["status"] == "EXPIRED" for proposal_id in expired)
    assert client.get("/v1/review/queue").status_code == 200
    with sqlite3.connect(api.store.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='query.expired'"
        ).fetchone()[0] == 100


def test_queue_excludes_proposal_expiring_between_sweep_and_list(api_client, monkeypatch):
    client, api = api_client
    proposal_id = make_pending(api)
    sweep = getattr(api.store, "expire_pending_approvals", None)
    assert sweep is not None

    def expire_after_sweep():
        result = sweep()
        with sqlite3.connect(api.store.path) as conn:
            conn.execute(
                "UPDATE proposals SET expires_at=datetime('now', '-1 second') WHERE proposal_id=?",
                (proposal_id,),
            )
        return result

    monkeypatch.setattr(api.store, "expire_pending_approvals", expire_after_sweep)
    assert client.get("/v1/review/queue").json()["items"] == []


def test_queue_excludes_proposal_expiring_after_list_snapshot(api_client, monkeypatch):
    client, api = api_client
    proposal_id = make_pending(api)
    expired_at = datetime.fromisoformat(api.store.get(proposal_id)["expires_at"])
    original = api.store.list_status

    def list_then_time_passes(*args, **kwargs):
        items = original(*args, **kwargs)
        monkeypatch.setattr(api, "now", lambda: expired_at, raising=False)
        return items

    monkeypatch.setattr(api.store, "list_status", list_then_time_passes)
    assert client.get("/v1/review/queue").json()["items"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=abc"])
def test_review_queue_pagination_bounds(api_client, query):
    client, _ = api_client
    assert client.get(f"/v1/review/queue?{query}").status_code == 422


def test_review_queue_pagination_has_stable_order(api_client):
    client, api = api_client
    proposal_ids = sorted(make_pending(api) for _ in range(3))
    with sqlite3.connect(api.store.path) as conn:
        conn.execute("UPDATE proposals SET created_at='2026-01-01T00:00:00+00:00'")
    for offset, proposal_id in enumerate(proposal_ids):
        response = client.get(f"/v1/review/queue?limit=1&offset={offset}")
        assert response.status_code == 200
        assert [p["proposal_id"] for p in response.json()["items"]] == [proposal_id]
    assert client.get("/v1/review/queue?limit=1&offset=3").json()["items"] == []
