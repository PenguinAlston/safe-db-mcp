from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from safe_db_mcp.config import DataSource
from safe_db_mcp.credentials import InMemoryCredentialManager
from safe_db_mcp.worker_ui import create_worker_ui_app


def make_manager():
    datasource = DataSource(
        "pg", "PostgreSQL", "postgres", None, "postgres_transaction",
        frozenset({"public.items"}), frozenset(), False,
        0, 0, 100, 10000, 1000, frozenset({"agent"}),
        "local_manual", 480,
    )
    return InMemoryCredentialManager(
        {"pg": datasource}, validator=lambda _datasource, _url: True
    )


def headers():
    return {
        "host": "127.0.0.1:8090",
        "origin": "http://127.0.0.1:8090",
    }


def login(client, password):
    response = client.post(
        "/api/login", headers=headers(), json={"password": password}
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_worker_ui_is_local_only_and_has_security_headers():
    client = TestClient(create_worker_ui_app(make_manager()))
    response = client.get("/", headers={"host": "127.0.0.1:8090"})
    assert response.status_code == 200
    assert "Content-Security-Policy" in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/", headers={"host": "example.com"}).status_code == 400


def test_worker_ui_rejects_cross_origin_writes():
    client = TestClient(create_worker_ui_app(make_manager()))
    response = client.post(
        "/api/login",
        headers={"host": "127.0.0.1:8090", "origin": "http://evil.example"},
        json={"password": "long-enough-secret"},
    )
    assert response.status_code == 403


def test_worker_ui_requires_local_password_before_login():
    client = TestClient(create_worker_ui_app(make_manager()))
    response = client.post(
        "/api/login", headers=headers(), json={"password": "long-enough-secret"}
    )
    assert response.status_code == 503


def test_worker_ui_authenticates_and_sets_httponly_strict_cookie():
    manager = make_manager()
    manager.set_ui_password("local-dashboard-password")
    client = TestClient(create_worker_ui_app(manager))
    response = client.post(
        "/api/login", headers=headers(), json={"password": "local-dashboard-password"}
    )
    csrf = response.json()["csrf_token"]
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert client.cookies.get("safedb_worker_session")
    assert csrf
    response = client.get("/api/status", headers={"host": "127.0.0.1:8090"})
    assert response.status_code == 200
    assert response.json()["datasources"][0]["state"] == "MISSING"


def test_worker_ui_rotates_and_clears_credentials_without_exposing_secret():
    manager = make_manager()
    manager.set_ui_password("local-dashboard-password")
    client = TestClient(create_worker_ui_app(manager))
    csrf = login(client, "local-dashboard-password")
    secret = "postgresql+psycopg://user:password@db.internal/app"
    response = client.post(
        "/api/credentials/pg",
        headers={**headers(), "x-safedb-csrf": csrf},
        json={
            "database_url": secret,
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()["status"]["state"] == "ACTIVE"
    status = client.get("/api/status", headers={"host": "127.0.0.1:8090"})
    assert secret not in status.text
    cleared = client.delete(
        "/api/credentials/pg",
        headers={**headers(), "x-safedb-csrf": csrf},
    )
    assert cleared.status_code == 200
    assert cleared.json()["status"]["state"] == "NEEDS_CREDENTIAL"


def test_worker_ui_blocks_credential_write_without_csrf():
    manager = make_manager()
    manager.set_ui_password("local-dashboard-password")
    client = TestClient(create_worker_ui_app(manager))
    login(client, "local-dashboard-password")
    response = client.post(
        "/api/credentials/pg",
        headers=headers(),
        json={
            "database_url": "postgresql+psycopg://user:password@db.internal/app",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
    )
    assert response.status_code == 403
    assert manager.status("pg")["state"] == "MISSING"


def test_worker_ui_rate_limits_failed_login_attempts():
    manager = make_manager()
    manager.set_ui_password("local-dashboard-password")
    client = TestClient(create_worker_ui_app(manager))
    for _ in range(5):
        assert client.post(
            "/api/login", headers=headers(), json={"password": "wrong-password"}
        ).status_code == 401
    assert client.post(
        "/api/login", headers=headers(), json={"password": "local-dashboard-password"}
    ).status_code == 429



def test_password_rotation_revokes_existing_dashboard_sessions():
    manager = make_manager()
    manager.set_ui_password("first-dashboard-password")
    client = TestClient(create_worker_ui_app(manager))
    login(client, "first-dashboard-password")
    assert client.get("/api/status", headers={"host": "127.0.0.1:8090"}).status_code == 200
    manager.set_ui_password("second-dashboard-password")
    response = client.get("/api/status", headers={"host": "127.0.0.1:8090"})
    assert response.status_code == 401
    assert login(client, "second-dashboard-password")
