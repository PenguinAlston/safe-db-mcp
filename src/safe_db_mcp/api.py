from __future__ import annotations

from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query

from safe_db_mcp.auth import require_agent, require_reviewer
from safe_db_mcp.config import load_datasources
from safe_db_mcp.models import DecisionRequest, PrepareQueryRequest, Principal
from safe_db_mcp.service import QueryService, now
from safe_db_mcp.store import ProposalStore

store = ProposalStore()
datasources = load_datasources()
service = QueryService(store, datasources)
app = FastAPI(title="SafeDB-MCP API", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/v1/datasources")
def list_datasources(principal: Principal = Depends(require_agent)):
    # No URL, secret reference, username or password is returned.
    return {
        "datasources": [
            {"datasource_id": ds.datasource_id, "name": ds.name, "dialect": ds.dialect}
            for ds in datasources.values()
            if principal.actor_id in ds.allowed_agents
        ]
    }


@app.post("/v1/queries")
def prepare_query(
    body: PrepareQueryRequest,
    principal: Principal = Depends(require_agent),
):
    try:
        return service.prepare(
            body.datasource_id, body.sql, body.purpose,
            body.parameters, principal.actor_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/queries/{proposal_id}")
def query_status(
    proposal_id: str,
    principal: Principal = Depends(require_agent),
):
    proposal = store.get(proposal_id)
    if proposal is None or proposal["requester_id"] != principal.actor_id:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return service.public_view(proposal)


@app.get("/v1/queries/{proposal_id}/result")
def query_result(
    proposal_id: str,
    principal: Principal = Depends(require_agent),
):
    proposal = store.get(proposal_id)
    if proposal is None or proposal["requester_id"] != principal.actor_id:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal["status"] != "SUCCEEDED" or not proposal.get("result"):
        raise HTTPException(status_code=409, detail="Result is not available")
    return proposal["result"]


@app.get("/v1/review/queue")
def review_queue(
    principal: Principal = Depends(require_reviewer),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    store.expire_pending_approvals()
    items = store.list_status("PENDING_APPROVAL", limit=limit, offset=offset)
    return {
        "items": [
            {
                **service.public_view(item),
                "purpose": item["purpose"],
                "sql": item["sql"],
                "parameters": item["parameters"],
                "sql_hash": item["sql_hash"],
                "requester_id": item["requester_id"],
            }
            for item in items
            if datetime.fromisoformat(item["expires_at"]) > now()
        ]
    }


@app.post("/v1/queries/{proposal_id}/decision")
def decide_query(
    proposal_id: str,
    body: DecisionRequest,
    principal: Principal = Depends(require_reviewer),
):
    proposal = store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    try:
        return service.decide(
            proposal, body.approve, principal.actor_id, body.reason, body.review_token
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
