from fastapi import HTTPException
import pytest
from starlette.requests import Request

from safe_db_mcp.auth import require_agent, require_reviewer


def make_request(authorization: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"authorization", authorization.encode("utf-8"))],
    })


@pytest.mark.parametrize("auth_fn,env_name", [
    (require_agent, "SAFEDB_AGENT_TOKEN"),
    (require_reviewer, "SAFEDB_REVIEWER_TOKEN"),
])
def test_non_ascii_bearer_token_is_a_normal_401(auth_fn, env_name, monkeypatch):
    monkeypatch.setenv(env_name, "valid-ascii-token")
    with pytest.raises(HTTPException) as exc:
        auth_fn(make_request("Bearer café"))
    assert exc.value.status_code == 401
