from __future__ import annotations

import logging
import os
import time

from safe_db_mcp.adapters.factory import AdapterUnavailable
from safe_db_mcp.config import load_datasources
from safe_db_mcp.service import QueryService, iso, now
from safe_db_mcp.store import ProposalStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("safedb.worker")


def run_once(store: ProposalStore, service: QueryService) -> int:
    lease_seconds = int(os.getenv("SAFEDB_WORKER_LEASE_SECONDS", "600"))
    if lease_seconds < 1:
        raise ValueError("SAFEDB_WORKER_LEASE_SECONDS must be positive")
    store.recover_stale_claims(lease_seconds)
    store.expire_pending_approvals()
    processed = 0

    for item in store.list_status("ANALYZING", limit=10):
        token = store.claim(item["proposal_id"], "ANALYZING", "ANALYZING_WORKER")
        if not token:
            continue
        item["status"] = "ANALYZING_WORKER"
        item["claim_token"] = token
        try:
            service.process_analysis(item)
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
        token = store.claim(item["proposal_id"], "APPROVED", "EXECUTING")
        if not token:
            continue
        item["status"] = "EXECUTING"
        item["claim_token"] = token
        try:
            service.process_execution(item)
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
    service = QueryService(store, load_datasources())
    interval = float(os.getenv("SAFEDB_WORKER_INTERVAL_SECONDS", "1"))
    if interval <= 0:
        raise ValueError("SAFEDB_WORKER_INTERVAL_SECONDS must be positive")
    logger.info("SafeDB worker started; database credentials are worker-only.")
    while True:
        run_once(store, service)
        time.sleep(interval)


if __name__ == "__main__":
    main()
