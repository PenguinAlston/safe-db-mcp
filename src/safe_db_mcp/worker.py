from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from safe_db_mcp.adapters.factory import AdapterUnavailable
from safe_db_mcp.credentials import (
    CredentialUnavailable,
    CredentialSocketServer,
    InMemoryCredentialManager,
)
from safe_db_mcp.config import load_datasources
from safe_db_mcp.service import QueryService, iso, now
from safe_db_mcp.store import ProposalStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("safedb.worker")


def _credential_available(service: QueryService, datasource_id: str) -> bool:
    if datasource_id not in service.datasources or service.credential_provider is None:
        return True
    return service.credential_provider.is_available(datasource_id)


def _proposal_expired(item: dict) -> bool:
    return datetime.fromisoformat(item["expires_at"]) <= now()


def _return_to_queue(store, item, token: str, expected_status: str, queued_status: str):
    store.update(
        item["proposal_id"],
        expected_claim_token=token,
        expected_status=expected_status,
        audit_event={
            "event_type": "query.waiting_for_credential",
            "actor_id": "worker",
            "created_at": iso(now()),
            "details": {"datasource_id": item["datasource_id"]},
        },
        status=queued_status,
    )


def run_once(store: ProposalStore, service: QueryService) -> int:
    lease_seconds = int(os.getenv("SAFEDB_WORKER_LEASE_SECONDS", "600"))
    if lease_seconds < 1:
        raise ValueError("SAFEDB_WORKER_LEASE_SECONDS must be positive")
    store.recover_stale_claims(lease_seconds)
    store.expire_pending_approvals()
    if service.credential_provider is not None:
        for status in service.credential_provider.refresh_statuses():
            level = "warning" if status["state"] == "EXPIRING" else "error"
            getattr(logger, level)(
                "Datasource %s credential state changed to %s (version=%s)",
                status["datasource_id"], status["state"], status["version"],
            )
    processed = 0

    for item in store.list_status("ANALYZING", limit=10):
        if (
            not _proposal_expired(item)
            and not _credential_available(service, item["datasource_id"])
        ):
            continue
        token = store.claim(item["proposal_id"], "ANALYZING", "ANALYZING_WORKER")
        if not token:
            continue
        item["status"] = "ANALYZING_WORKER"
        item["claim_token"] = token
        try:
            service.process_analysis(item)
        except CredentialUnavailable:
            _return_to_queue(
                store, item, token, "ANALYZING_WORKER", "ANALYZING"
            )
        except Exception as exc:
            blocked = isinstance(exc, AdapterUnavailable)
            store.update(
                item["proposal_id"],
                expected_claim_token=token,
                expected_status="ANALYZING_WORKER",
                audit_event={
                    "event_type": "query.analysis_failed",
                    "actor_id": "worker",
                    "created_at": iso(now()),
                    "details": {"exception_type": type(exc).__name__},
                },
                status="BLOCKED" if blocked else "FAILED",
                error_code="ADAPTER_UNAVAILABLE" if blocked else "ANALYSIS_FAILED",
            )
            # Do not log exception text: database drivers can include DSNs/SQL values.
            logger.error(
                "Analysis failed for proposal %s (%s)",
                item["proposal_id"], type(exc).__name__,
            )
        processed += 1

    for item in store.list_status("APPROVED", limit=10):
        if (
            not _proposal_expired(item)
            and not _credential_available(service, item["datasource_id"])
        ):
            continue
        token = store.claim(item["proposal_id"], "APPROVED", "EXECUTING")
        if not token:
            continue
        item["status"] = "EXECUTING"
        item["claim_token"] = token
        try:
            service.process_execution(item)
        except CredentialUnavailable:
            _return_to_queue(
                store, item, token, "EXECUTING", "APPROVED"
            )
        except Exception as exc:
            blocked = isinstance(exc, AdapterUnavailable)
            store.update(
                item["proposal_id"],
                expected_claim_token=token,
                expected_status="EXECUTING",
                audit_event={
                    "event_type": "query.execution_failed",
                    "actor_id": "worker",
                    "created_at": iso(now()),
                    "details": {"exception_type": type(exc).__name__},
                },
                status="BLOCKED" if blocked else "FAILED",
                error_code="ADAPTER_UNAVAILABLE" if blocked else "EXECUTION_FAILED",
            )
            logger.error(
                "Execution failed for proposal %s (%s)",
                item["proposal_id"], type(exc).__name__,
            )
        processed += 1
    return processed


def main():
    store = ProposalStore()
    datasources = load_datasources()
    credential_manager = InMemoryCredentialManager(datasources)
    socket_path = os.getenv(
        "SAFEDB_CREDENTIAL_SOCKET", "./run/safedb/credentials.sock"
    )
    credential_server = CredentialSocketServer(credential_manager, socket_path)
    credential_server.start()
    service = QueryService(store, datasources, credential_manager)
    interval = float(os.getenv("SAFEDB_WORKER_INTERVAL_SECONDS", "1"))
    if interval <= 0:
        credential_server.close()
        raise ValueError("SAFEDB_WORKER_INTERVAL_SECONDS must be positive")
    logger.info("SafeDB worker started; target database credentials remain worker-local.")
    logger.info("Local credential management socket is ready.")
    try:
        while True:
            run_once(store, service)
            time.sleep(interval)
    finally:
        credential_server.close()


if __name__ == "__main__":
    main()
