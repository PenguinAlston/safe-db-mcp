import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from safe_db_mcp import auth
from safe_db_mcp.auth import AuthenticationError, OidcVerifier


ISSUER = "https://idp.test/realms/safedb"
AUDIENCE = "safedb-api"


@pytest.fixture(scope="module")
def signing_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from jwt.algorithms import RSAAlgorithm

    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update({"kid": "test-key-1", "alg": "RS256", "use": "sig"})
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, jwk, other


class _Handler(BaseHTTPRequestHandler):
    jwk: dict = {}
    fail = False

    def do_GET(self):  # noqa: N802
        if self.fail:
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({
            "issuer": ISSUER,
            "jwks_uri": f"http://127.0.0.1:{self.server.server_port}/jwks.json",
        } if self.path == "/.well-known/openid-configuration" else {"keys": [self.jwk]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def jwks_server(signing_material):
    global ISSUER
    _, jwk, _ = signing_material
    _Handler.jwk = jwk
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ISSUER = f"http://127.0.0.1:{server.server_port}"
    yield ISSUER
    server.shutdown()


@pytest.fixture
def verifier(jwks_server):
    return OidcVerifier(
        issuer=jwks_server, audience=AUDIENCE,
        agent_scope="safedb:query", reviewer_scope="safedb:review",
    )


def mint(key, **claims):
    payload = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": "agent-alpha",
        "scope": "safedb:query", "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    payload.update(claims)
    headers = {"kid": "test-key-1"} if key is not None else {}
    return jwt.encode(payload, key, algorithm="RS256", headers=headers)


def test_valid_agent_token_maps_sub_to_actor_id(verifier, signing_material):
    key, _, _ = signing_material
    principal = verifier.verify(mint(key), role="agent")
    assert principal.role == "agent"
    assert principal.actor_id == "agent-alpha"


def test_expired_token_is_rejected(verifier, signing_material):
    key, _, _ = signing_material
    with pytest.raises(AuthenticationError):
        verifier.verify(mint(key, exp=int(time.time()) - 10), role="agent")


def test_wrong_audience_is_rejected(verifier, signing_material):
    key, _, _ = signing_material
    with pytest.raises(AuthenticationError):
        verifier.verify(mint(key, aud="other-service"), role="agent")


def test_wrong_issuer_is_rejected(jwks_server, signing_material):
    key, _, _ = signing_material
    verifier = OidcVerifier(
        issuer=jwks_server, audience=AUDIENCE,
        agent_scope="safedb:query", reviewer_scope="safedb:review",
    )
    with pytest.raises(AuthenticationError):
        verifier.verify(mint(key, iss="https://evil.test"), role="agent")


def test_agent_token_without_agent_scope_is_rejected(verifier, signing_material):
    key, _, _ = signing_material
    with pytest.raises(AuthenticationError):
        verifier.verify(mint(key, scope="openid profile"), role="agent")


def test_reviewer_only_token_cannot_act_as_agent(verifier, signing_material):
    key, _, _ = signing_material
    with pytest.raises(AuthenticationError):
        verifier.verify(mint(key, scope="safedb:review"), role="agent")


def test_dual_scope_token_is_rejected_for_both_roles(verifier, signing_material):
    key, _, _ = signing_material
    token = mint(key, scope="safedb:query safedb:review")
    with pytest.raises(AuthenticationError):
        verifier.verify(token, role="agent")
    with pytest.raises(AuthenticationError):
        verifier.verify(token, role="reviewer")


def test_reviewer_token_with_reviewer_scope_is_accepted(verifier, signing_material):
    key, _, _ = signing_material
    principal = verifier.verify(mint(key, sub="reviewer-1", scope="safedb:review"),
                                role="reviewer")
    assert principal.role == "reviewer"
    assert principal.actor_id == "reviewer-1"


def test_scp_list_claim_is_accepted(verifier, signing_material):
    key, _, _ = signing_material
    principal = verifier.verify(mint(key, scope=None, scp=["safedb:query"]),
                                role="agent")
    assert principal.actor_id == "agent-alpha"


def test_tampered_signature_is_rejected(verifier, signing_material):
    key, _, _ = signing_material
    token = mint(key)
    header, payload, signature = token.split(".")
    with pytest.raises(AuthenticationError):
        verifier.verify(f"{header}.{payload}.{'A' + signature[1:]}", role="agent")


def test_key_not_in_jwks_is_rejected(verifier, signing_material):
    _, _, other = signing_material
    with pytest.raises(AuthenticationError):
        verifier.verify(mint(other), role="agent")


def test_jwks_outage_fails_closed(jwks_server, signing_material):
    key, _, _ = signing_material
    verifier = OidcVerifier(
        issuer=jwks_server, audience=AUDIENCE,
        agent_scope="safedb:query", reviewer_scope="safedb:review",
    )
    _Handler.fail = True
    try:
        with pytest.raises(AuthenticationError):
            verifier.verify(mint(key), role="agent")
    finally:
        _Handler.fail = False


def test_actor_claim_is_configurable(jwks_server, signing_material):
    key, _, _ = signing_material
    verifier = OidcVerifier(
        issuer=jwks_server, audience=AUDIENCE,
        agent_scope="safedb:query", reviewer_scope="safedb:review",
        actor_claim="client_id",
    )
    assert verifier.verify(mint(key, client_id="svc-agent"), role="agent").actor_id == (
        "svc-agent"
    )


def test_reviewer_acr_requirement_when_configured(jwks_server, signing_material):
    key, _, _ = signing_material
    verifier = OidcVerifier(
        issuer=jwks_server, audience=AUDIENCE,
        agent_scope="safedb:query", reviewer_scope="safedb:review",
        reviewer_acr="mfa",
    )
    good = mint(key, sub="reviewer-1", scope="safedb:review", acr="mfa")
    weak = mint(key, sub="reviewer-1", scope="safedb:review", acr="pwd")
    none_acr = mint(key, sub="reviewer-1", scope="safedb:review")
    with pytest.raises(AuthenticationError):
        verifier.verify(weak, role="reviewer")
    with pytest.raises(AuthenticationError):
        verifier.verify(none_acr, role="reviewer")
    assert verifier.verify(good, role="reviewer").actor_id == "reviewer-1"


def test_require_agent_enforces_oidc_when_issuer_configured(
    monkeypatch, jwks_server, signing_material
):
    key, _, _ = signing_material
    monkeypatch.setenv("SAFEDB_OIDC_ISSUER", jwks_server)
    monkeypatch.setenv("SAFEDB_OIDC_AUDIENCE", AUDIENCE)
    auth.set_verifier(None)
    try:
        with pytest.raises(HTTPException) as exc:
            auth.require_agent(_request("dev-static-token"))
        assert exc.value.status_code == 401
        principal = auth.require_agent(_request(mint(key)))
        assert principal.actor_id == "agent-alpha"
        assert principal.role == "agent"
    finally:
        auth.set_verifier(None)


def _request(token: str):
    from starlette.requests import Request

    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })
