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


@pytest.mark.parametrize("shared_field", ["token", "identity"])
def test_reviewer_rejects_shared_agent_credentials(monkeypatch, shared_field):
    monkeypatch.setenv("SAFEDB_AGENT_TOKEN", "test-agent-token")
    monkeypatch.setenv("SAFEDB_REVIEWER_TOKEN", "test-reviewer-token")
    monkeypatch.setenv("SAFEDB_AGENT_ID", "test-agent")
    monkeypatch.setenv("SAFEDB_REVIEWER_ID", "test-reviewer")
    token = "test-reviewer-token"
    if shared_field == "token":
        token = "test-agent-token"
        monkeypatch.setenv("SAFEDB_REVIEWER_TOKEN", token)
    else:
        monkeypatch.setenv("SAFEDB_REVIEWER_ID", "test-agent")
    with pytest.raises(HTTPException) as error:
        require_reviewer(make_request(f"Bearer {token}"))
    assert error.value.status_code == 503


def test_independent_reviewer_credentials_are_accepted(monkeypatch):
    monkeypatch.setenv("SAFEDB_AGENT_TOKEN", "test-agent-token")
    monkeypatch.setenv("SAFEDB_REVIEWER_TOKEN", "test-reviewer-token")
    monkeypatch.setenv("SAFEDB_AGENT_ID", "test-agent")
    monkeypatch.setenv("SAFEDB_REVIEWER_ID", "test-reviewer")
    assert require_reviewer(make_request("Bearer test-reviewer-token")).actor_id == (
        "test-reviewer"
    )
