from __future__ import annotations

import hmac
import logging
import os
import threading

import httpx
import jwt
from fastapi import HTTPException, Request

from safe_db_mcp.models import Principal

logger = logging.getLogger("safedb.auth")


class AuthenticationError(Exception):
    """The presented credential is invalid, expired, or lacks the required role."""


class MisconfigurationError(Exception):
    """The deployment's authentication settings are unsafe or inconsistent."""


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


class StaticTokenVerifier:
    # Environment-based bearer tokens are for local development only.
    def __init__(self) -> None:
        self._warned = False

    def verify(self, token: str, role: str) -> Principal:
        if not self._warned:
            logger.warning(
                "Static token authentication is active; configure SAFEDB_OIDC_ISSUER "
                "for any shared or production deployment."
            )
            self._warned = True
        if role == "agent":
            expected = os.getenv("SAFEDB_AGENT_TOKEN", "")
            actor_id = os.getenv("SAFEDB_AGENT_ID", "agent-dev")
        else:
            expected = os.getenv("SAFEDB_REVIEWER_TOKEN", "")
            actor_id = os.getenv("SAFEDB_REVIEWER_ID", "reviewer-dev")
        if not expected or not _token_matches(token, expected):
            raise AuthenticationError(f"Invalid {role} token")
        if role == "reviewer" and (
            _token_matches(expected, os.getenv("SAFEDB_AGENT_TOKEN", ""))
            or actor_id == os.getenv("SAFEDB_AGENT_ID", "agent-dev")
        ):
            raise MisconfigurationError(
                "Reviewer credentials must be independent of agent credentials"
            )
        return Principal(actor_id=actor_id, role=role)


class _JwksClient(jwt.PyJWKClient):
    # PyJWKClient's urllib fetch honors ambient proxy settings; an internal IdP
    # must not be sent through them. httpx with trust_env=False keeps key
    # caching and unknown-kid refetch while bypassing proxy configuration.
    def fetch_data(self) -> jwt.PyJWKSet:
        response = httpx.get(self.uri, trust_env=False, timeout=10)
        response.raise_for_status()
        return jwt.PyJWKSet.from_dict(response.json())


class OidcVerifier:
    # RS256/ES256 only: asymmetric algorithms prevent HMAC algorithm-confusion.
    _ALGORITHMS = ["RS256", "ES256"]

    def __init__(
        self,
        issuer: str,
        audience: str,
        agent_scope: str,
        reviewer_scope: str,
        actor_claim: str = "sub",
        reviewer_acr: str | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._agent_scope = agent_scope
        self._reviewer_scope = reviewer_scope
        self._actor_claim = actor_claim
        self._reviewer_acrs = (
            {value.strip() for value in reviewer_acr.split(",") if value.strip()}
            if reviewer_acr else None
        )
        self._jwks_client: _JwksClient | None = None
        self._discovery_lock = threading.Lock()

    def _jwks_client_or_raise(self) -> _JwksClient:
        if self._jwks_client is None:
            with self._discovery_lock:
                if self._jwks_client is None:
                    response = httpx.get(
                        f"{self._issuer}/.well-known/openid-configuration",
                        trust_env=False, timeout=10,
                    )
                    response.raise_for_status()
                    jwks_uri = response.json()["jwks_uri"]
                    if not isinstance(jwks_uri, str) or not jwks_uri:
                        raise ValueError("Discovery document has no jwks_uri")
                    self._jwks_client = _JwksClient(jwks_uri)
        return self._jwks_client

    @staticmethod
    def _scopes(claims: dict) -> set[str]:
        scope = claims.get("scope")
        if isinstance(scope, str):
            return set(scope.split())
        scp = claims.get("scp")
        if isinstance(scp, list):
            return {entry for entry in scp if isinstance(entry, str)}
        return set()

    def verify(self, token: str, role: str) -> Principal:
        try:
            client = self._jwks_client_or_raise()
            signing_key = client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError("Token verification failed") from exc

        if role == "reviewer" and self._reviewer_acrs is not None:
            if claims.get("acr") not in self._reviewer_acrs:
                raise AuthenticationError("Reviewer authentication level not satisfied")

        scopes = self._scopes(claims)
        required, forbidden = (
            (self._agent_scope, self._reviewer_scope) if role == "agent"
            else (self._reviewer_scope, self._agent_scope)
        )
        # Roles are structurally separated: a token carrying both scopes is
        # rejected either way, so one credential can never hold both duties.
        if required not in scopes or forbidden in scopes:
            raise AuthenticationError("Token lacks the required role scope")

        actor_id = claims.get(self._actor_claim)
        if not isinstance(actor_id, str) or not actor_id:
            raise AuthenticationError("Token carries no actor identity claim")
        return Principal(actor_id=actor_id, role=role)


def _default_verifier():
    issuer = os.getenv("SAFEDB_OIDC_ISSUER", "")
    if not issuer:
        return StaticTokenVerifier()
    audience = os.getenv("SAFEDB_OIDC_AUDIENCE", "")
    if not audience:
        raise MisconfigurationError(
            "SAFEDB_OIDC_AUDIENCE must be set when SAFEDB_OIDC_ISSUER is configured"
        )
    return OidcVerifier(
        issuer=issuer,
        audience=audience,
        agent_scope=os.getenv("SAFEDB_OIDC_AGENT_SCOPE", "safedb:query"),
        reviewer_scope=os.getenv("SAFEDB_OIDC_REVIEWER_SCOPE", "safedb:review"),
        actor_claim=os.getenv("SAFEDB_OIDC_ACTOR_CLAIM", "sub"),
        reviewer_acr=os.getenv("SAFEDB_OIDC_REVIEWER_ACR", "") or None,
    )


_verifier: StaticTokenVerifier | OidcVerifier | None = None


def set_verifier(verifier) -> None:
    global _verifier
    _verifier = verifier


def _get_verifier():
    global _verifier
    if _verifier is None:
        _verifier = _default_verifier()
    return _verifier


def _verify(token: str, role: str) -> Principal:
    try:
        return _get_verifier().verify(token, role)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Authentication failed") from exc
    except MisconfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def require_agent(request: Request) -> Principal:
    return _verify(_bearer(request), "agent")


def require_reviewer(request: Request) -> Principal:
    return _verify(_bearer(request), "reviewer")
