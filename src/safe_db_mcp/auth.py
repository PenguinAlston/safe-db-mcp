from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

from safe_db_mcp.models import Principal


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    return token


def require_agent(request: Request) -> Principal:
    expected = os.getenv("SAFEDB_AGENT_TOKEN", "")
    if not expected or not hmac.compare_digest(_bearer(request), expected):
        raise HTTPException(status_code=401, detail="Invalid Agent token")
    return Principal(
        actor_id=os.getenv("SAFEDB_AGENT_ID", "agent-dev"),
        role="agent",
    )


def require_reviewer(request: Request) -> Principal:
    expected = os.getenv("SAFEDB_REVIEWER_TOKEN", "")
    if not expected or not hmac.compare_digest(_bearer(request), expected):
        raise HTTPException(status_code=401, detail="Invalid reviewer token")
    return Principal(
        actor_id=os.getenv("SAFEDB_REVIEWER_ID", "reviewer-dev"),
        role="reviewer",
    )
