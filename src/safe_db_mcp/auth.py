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


def _token_matches(supplied: str, expected: str) -> bool:
    # compare_digest(str, str) accepts ASCII only. Compare UTF-8 byte strings
    # so a malformed/non-ASCII token is a normal authentication failure (401).
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def require_agent(request: Request) -> Principal:
    expected = os.getenv("SAFEDB_AGENT_TOKEN", "")
    if not expected or not _token_matches(_bearer(request), expected):
        raise HTTPException(status_code=401, detail="Invalid Agent token")
    return Principal(
        actor_id=os.getenv("SAFEDB_AGENT_ID", "agent-dev"),
        role="agent",
    )


def require_reviewer(request: Request) -> Principal:
    expected = os.getenv("SAFEDB_REVIEWER_TOKEN", "")
    if not expected or not _token_matches(_bearer(request), expected):
        raise HTTPException(status_code=401, detail="Invalid reviewer token")
    return Principal(
        actor_id=os.getenv("SAFEDB_REVIEWER_ID", "reviewer-dev"),
        role="reviewer",
    )
