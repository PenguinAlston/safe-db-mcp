from __future__ import annotations

import socket
import time
from datetime import UTC, datetime, timedelta

import pytest

from safe_db_mcp.config import DataSource
from safe_db_mcp.credentials import (
    CredentialSocketClient,
    CredentialSocketServer,
    CredentialUnavailable,
    InMemoryCredentialManager,
)


def datasource(max_ttl=480):
    return DataSource(
        "pg", "PostgreSQL", "postgres", None, "postgres_transaction",
        frozenset({"public.items"}), frozenset(), False,
        0, 0, 100, 10000, 1000, frozenset({"agent"}),
        "local_manual", max_ttl,
    )


def manager():
    return InMemoryCredentialManager({"pg": datasource()})


def test_manual_credential_is_available_and_status_never_contains_secret():
    credentials = manager()
    secret = "postgresql+psycopg://user:password@db.internal:5432/app"
    status = credentials.set_credential(
        "pg", secret, datetime.now(UTC) + timedelta(hours=7)
    )
    assert status["state"] == "ACTIVE"
    assert status["version"] == 1
    assert credentials.is_available("pg")
    assert credentials.get_url("pg") == secret
    assert secret not in repr(status)
    assert credentials.status("pg")["expires_at"]


def test_missing_credential_is_not_available():
    credentials = manager()
    assert not credentials.is_available("pg")
    assert credentials.status("pg")["state"] == "MISSING"
    with pytest.raises(CredentialUnavailable):
        credentials.get_url("pg")


def test_expired_credential_is_erased_and_fails_closed():
    credentials = manager()
    credentials.set_credential(
        "pg", "postgresql+psycopg://user:password@db.internal/app",
        datetime.now(UTC) + timedelta(milliseconds=10),
    )
    assert credentials.is_available("pg")
    time.sleep(0.03)
    assert not credentials.is_available("pg")
    assert credentials.status("pg")["state"] == "NEEDS_CREDENTIAL"
    with pytest.raises(CredentialUnavailable):
        credentials.get_url("pg")


def test_expiring_state_is_reported_before_expiry():
    credentials = manager()
    credentials.set_credential(
        "pg", "postgresql+psycopg://user:password@db.internal/app",
        datetime.now(UTC) + timedelta(minutes=20),
    )
    status = credentials.status("pg")
    assert status["state"] == "EXPIRING"
    assert credentials.is_available("pg")


def test_replacement_increments_version_and_clear_removes_secret():
    credentials = manager()
    first = "postgresql+psycopg://user:first@db.internal/app"
    second = "postgresql+psycopg://user:second@db.internal/app"
    expiry = datetime.now(UTC) + timedelta(hours=7)
    assert credentials.set_credential("pg", first, expiry)["version"] == 1
    assert credentials.set_credential("pg", second, expiry)["version"] == 2
    assert credentials.get_url("pg") == second
    assert credentials.clear_credential("pg")["state"] == "NEEDS_CREDENTIAL"
    with pytest.raises(CredentialUnavailable):
        credentials.get_url("pg")


@pytest.mark.parametrize(
    "url",
    [
        "mysql+pymysql://user:password@db.internal/app",
        "not-a-url",
        "postgresql+psycopg://user:password@db.internal/app\n",
        "",
    ],
)
def test_invalid_or_wrong_dialect_urls_are_rejected(url):
    with pytest.raises(ValueError):
        manager().set_credential("pg", url, datetime.now(UTC) + timedelta(hours=1))


def test_credential_ttl_limit_is_enforced():
    with pytest.raises(ValueError, match="TTL limit"):
        manager().set_credential(
            "pg", "postgresql+psycopg://user:password@db.internal/app",
            datetime.now(UTC) + timedelta(hours=9),
        )


def test_timezone_is_required_for_expiry():
    with pytest.raises(ValueError, match="timezone"):
        manager().set_credential(
            "pg", "postgresql+psycopg://user:password@db.internal/app",
            datetime.now() + timedelta(hours=1),
        )


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets required")
def test_local_socket_set_status_and_clear_roundtrip(tmp_path):
    credentials = manager()
    socket_path = tmp_path / "private" / "credentials.sock"
    server = CredentialSocketServer(credentials, str(socket_path))
    server.start()
    thread = server._thread
    assert thread is not None and thread.is_alive()
    client = CredentialSocketClient(str(socket_path))
    secret = "postgresql+psycopg://user:password@db.internal/app"
    try:
        response = client.request({
            "operation": "set",
            "datasource_id": "pg",
            "database_url": secret,
            "expires_at": (datetime.now(UTC) + timedelta(hours=7)).isoformat(),
        })
        assert response["status"]["version"] == 1
        status = client.request({"operation": "status"})["statuses"][0]
        assert status["state"] == "ACTIVE"
        assert secret not in repr(status)
        client.request({"operation": "clear", "datasource_id": "pg"})
        assert credentials.status("pg")["state"] == "NEEDS_CREDENTIAL"
    finally:
        server.close()


def test_env_credential_datasource_is_not_managed_by_local_store():
    env_datasource = DataSource(
        "mysql", "MySQL", "mysql", "MYSQL_URL", "mysql_transaction",
        frozenset({"sales.orders"}), frozenset(), False,
        0, 0, 100, 10000, 1000, frozenset({"agent"}),
    )
    credentials = InMemoryCredentialManager({"mysql": env_datasource})
    assert credentials.is_available("mysql")
    assert credentials.status("mysql")["state"] == "EXTERNAL"



def test_failed_candidate_validation_preserves_previous_credential():
    def validator(datasource, database_url):
        return database_url.endswith("/valid")

    credentials = InMemoryCredentialManager(
        {"pg": datasource()}, validator=validator
    )
    expiry = datetime.now(UTC) + timedelta(hours=2)
    first = "postgresql+psycopg://user:secret@db.internal/valid"
    rejected = "postgresql+psycopg://user:secret@db.internal/invalid"
    credentials.set_credential("pg", first, expiry)
    with pytest.raises(ValueError, match="validation failed"):
        credentials.set_credential("pg", rejected, expiry)
    assert credentials.get_url("pg") == first
    assert credentials.status("pg")["version"] == 1


def test_candidate_validator_receives_datasource_and_is_required_before_rotation():
    calls = []

    def validator(datasource_arg, database_url):
        calls.append((datasource_arg.datasource_id, database_url))
        return True

    credentials = InMemoryCredentialManager(
        {"pg": datasource()}, validator=validator
    )
    secret = "postgresql+psycopg://user:password@db.internal/app"
    credentials.set_credential(
        "pg", secret, datetime.now(UTC) + timedelta(hours=2)
    )
    assert calls == [("pg", secret)]
