from __future__ import annotations

import logging
import os
import time

from safe_db_mcp.adapters.factory import AdapterUnavailable
from safe_db_mcp.config import load_datasources
from safe_db_mcp.service import QueryService
from safe_db_mcp.store import ProposalStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("safedb.worker")


def run_once(store: ProposalStore, service: QueryService) -> int:
    lease_seconds = int(os.getenv("SAFEDB_WORKER_LEASE_SECONDS", "600"))
    store.recover_stale_claims(lease_seconds)
    processed = 0
    for item in store.list_status("ANALYZING", limit=10):
        if not store.claim(item["proposal_id"], "ANALYZING", "ANALYZING_WORKER"):
            continue
        try:
            item["status"] = "ANALYZING_WORKER"
            service.process_analysis(item)
        except Exception as exc:
            blocked = isinstance(exc, AdapterUnavailable)
            store.update(
                item["proposal_id"],
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
        if not store.claim(item["proposal_id"], "APPROVED", "EXECUTING"):
            continue
        try:
            item["status"] = "EXECUTING"
            service.process_execution(item)
        except Exception as exc:
            blocked = isinstance(exc, AdapterUnavailable)
            store.update(
                item["proposal_id"],
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
    logger.info("SafeDB worker started; database credentials are worker-only.")
    while True:
        run_once(store, service)
        time.sleep(interval)


if __name__ == "__main__":
    main()
