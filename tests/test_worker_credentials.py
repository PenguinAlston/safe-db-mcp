from __future__ import annotations

from safe_db_mcp.worker import _credential_available


class MissingCredentialProvider:
    def is_available(self, datasource_id):
        return False


class AvailableCredentialProvider:
    def is_available(self, datasource_id):
        return datasource_id == "ready"


def test_worker_skips_datasource_without_local_credential():
    class Service:
        credential_provider = MissingCredentialProvider()
        datasources = {"db": object()}

    assert not _credential_available(Service(), "db")


def test_worker_allows_configured_datasource_with_credential():
    class Service:
        credential_provider = AvailableCredentialProvider()
        datasources = {"ready": object()}

    assert _credential_available(Service(), "ready")


def test_worker_keeps_legacy_environment_credential_mode():
    class Service:
        credential_provider = None
        datasources = {"db": object()}

    assert _credential_available(Service(), "db")
