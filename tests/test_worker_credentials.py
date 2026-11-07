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



def _make_manual_service(tmp_path):
    from safe_db_mcp.config import DataSource
    from safe_db_mcp.credentials import InMemoryCredentialManager
    from safe_db_mcp.service import QueryService
    from safe_db_mcp.store import ProposalStore

    datasource = DataSource(
        "db", "test", "postgres", None, "postgres_transaction",
        frozenset({"public.items"}), frozenset(), False,
        0, 0, 100, 10000, 1000, frozenset({"agent"}),
        "local_manual", 480,
    )
    store = ProposalStore(str(tmp_path / "queue.sqlite3"))
    manager = InMemoryCredentialManager({"db": datasource})
    service = QueryService(store, {"db": datasource}, manager)
    proposal_id = service.prepare(
        "db", "SELECT id FROM public.items LIMIT 1", "test", {}, "agent"
    )["proposal_id"]
    return store, manager, service, proposal_id


def test_run_once_leaves_query_queued_when_manual_credential_is_missing(tmp_path):
    from safe_db_mcp.worker import run_once

    store, manager, service, proposal_id = _make_manual_service(tmp_path)
    assert run_once(store, service) == 0
    assert store.get(proposal_id)["status"] == "ANALYZING"
    assert not manager.is_available("db")


def test_run_once_keeps_approved_query_queued_without_credential(tmp_path):
    from safe_db_mcp.worker import run_once

    store, manager, service, proposal_id = _make_manual_service(tmp_path)
    assert store.update(
        proposal_id,
        expected_status="ANALYZING",
        status="APPROVED",
    )
    assert run_once(store, service) == 0
    assert store.get(proposal_id)["status"] == "APPROVED"
    assert not manager.is_available("db")


def test_credential_expiry_race_returns_analysis_to_queue(tmp_path, monkeypatch):
    from safe_db_mcp.credentials import CredentialUnavailable
    from safe_db_mcp.worker import run_once

    store, manager, service, proposal_id = _make_manual_service(tmp_path)
    manager.set_credential(
        "db",
        "postgresql+psycopg://user:password@db.internal/app",
        __import__("datetime").datetime.now(__import__("datetime").UTC)
        + __import__("datetime").timedelta(hours=1),
    )
    monkeypatch.setattr(manager, "is_available", lambda datasource_id: True)
    monkeypatch.setattr(
        "safe_db_mcp.adapters.factory.create_adapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(CredentialUnavailable()),
    )
    assert run_once(store, service) == 1
    assert store.get(proposal_id)["status"] == "ANALYZING"
