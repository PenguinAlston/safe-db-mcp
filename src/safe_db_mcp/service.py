from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from safe_db_mcp.config import DataSource
from safe_db_mcp.security.risk import assess_risk
from safe_db_mcp.security.sql_guard import validate_sql
from safe_db_mcp.store import ProposalStore


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat()


class QueryService:
    def __init__(self, store: ProposalStore, datasources: dict[str, DataSource]):
        self.store = store
        self.datasources = datasources

    def prepare(
        self, datasource_id: str, sql: str, purpose: str,
        parameters: dict[str, Any], requester_id: str
    ) -> dict:
        datasource = self.datasources.get(datasource_id)
        if datasource is None:
            raise PermissionError("Datasource is not registered")
        query = validate_sql(sql, datasource, parameters)
        created = now()
        proposal = {
            "proposal_id": str(uuid.uuid4()),
            "datasource_id": datasource_id,
            "requester_id": requester_id,
            "purpose": purpose,
            "sql": sql,
            "parameters": parameters,
            "sql_hash": query.sql_hash,
            "status": "ANALYZING",
            "created_at": iso(created),
            "expires_at": iso(created + timedelta(minutes=15)),
        }
        self.store.create(proposal)
        self.store.audit(
            proposal["proposal_id"], "query.submitted", requester_id,
            proposal["created_at"], {
                "datasource_id": datasource_id,
                "sql_hash": query.sql_hash,
            },
        )
        return self.public_view(self.store.get(proposal["proposal_id"]))

    def process_analysis(self, proposal: dict) -> None:
        if datetime.fromisoformat(proposal["expires_at"]) <= now():
            self.store.update(proposal["proposal_id"], status="EXPIRED")
            return
        datasource = self.datasources[proposal["datasource_id"]]
        query = validate_sql(proposal["sql"], datasource, proposal["parameters"])
        if query.sql_hash != proposal["sql_hash"]:
            self._block(proposal, "QUERY_HASH_MISMATCH")
            return

        from safe_db_mcp.adapters.factory import create_adapter
        adapter = create_adapter(datasource)
        try:
            readonly = adapter.verify_readonly()
            if not readonly:
                self._block(proposal, "READONLY_NOT_VERIFIED")
                return
            plan = adapter.explain(proposal["sql"], proposal["parameters"])
            assessment = assess_risk(query, plan, datasource, readonly)
            if (
                plan.estimated_scan_rows is not None
                and datasource.max_hard_scan_rows > 0
                and plan.estimated_scan_rows > datasource.max_hard_scan_rows
            ):
                self._block(
                    proposal, "HARD_SCAN_LIMIT_EXCEEDED", assessment.model_dump()
                )
                return

            if assessment.auto_eligible:
                self._execute(proposal, adapter, datasource, assessment)
                return

            self.store.update(
                proposal["proposal_id"],
                status="PENDING_APPROVAL",
                risk_json=assessment.model_dump(),
            )
            self.store.audit(
                proposal["proposal_id"], "query.pending_approval", "worker",
                iso(now()), {
                    "risk_level": assessment.level,
                    "risk_score": assessment.score,
                },
            )
        finally:
            adapter.close()

    def process_execution(self, proposal: dict) -> None:
        if datetime.fromisoformat(proposal["expires_at"]) <= now():
            self.store.update(proposal["proposal_id"], status="EXPIRED")
            return
        approval = proposal.get("approval") or {}
        if (
            approval.get("decision") != "APPROVED"
            or approval.get("sql_hash") != proposal["sql_hash"]
        ):
            self._block(proposal, "APPROVAL_BINDING_INVALID")
            return

        datasource = self.datasources[proposal["datasource_id"]]
        query = validate_sql(proposal["sql"], datasource, proposal["parameters"])
        if query.sql_hash != proposal["sql_hash"]:
            self._block(proposal, "QUERY_HASH_MISMATCH")
            return

        from safe_db_mcp.adapters.factory import create_adapter
        adapter = create_adapter(datasource)
        try:
            if not adapter.verify_readonly():
                self._block(proposal, "READONLY_NOT_VERIFIED")
                return
            plan = adapter.explain(proposal["sql"], proposal["parameters"])
            assessment = assess_risk(query, plan, datasource, True)
            old = proposal.get("risk") or {}
            old_level = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(old.get("level"), 2)
            new_level = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[assessment.level]
            if new_level > old_level:
                self.store.update(
                    proposal["proposal_id"],
                    status="NEEDS_REAPPROVAL",
                    risk_json=assessment.model_dump(),
                )
                self.store.audit(
                    proposal["proposal_id"], "query.risk_increased", "worker",
                    iso(now()), {"new_risk_level": assessment.level},
                )
                return
            if (
                plan.estimated_scan_rows is not None
                and datasource.max_hard_scan_rows > 0
                and plan.estimated_scan_rows > datasource.max_hard_scan_rows
            ):
                self._block(
                    proposal, "HARD_SCAN_LIMIT_EXCEEDED", assessment.model_dump()
                )
                return
            self._execute(proposal, adapter, datasource, assessment)
        finally:
            adapter.close()

    def _execute(self, proposal, adapter, datasource, assessment):
        result = adapter.execute_readonly(
            proposal["sql"], proposal["parameters"],
            datasource.max_result_rows, datasource.max_result_bytes,
            datasource.timeout_ms,
        )
        self.store.update(
            proposal["proposal_id"],
            status="SUCCEEDED",
            risk_json=assessment.model_dump(),
            result_json=result,
            error_code=None,
        )
        self.store.audit(
            proposal["proposal_id"], "query.succeeded", "worker", iso(now()), {
                "returned_rows": result["returned_rows"],
                "result_bytes": result["result_bytes"],
                "duration_ms": result["duration_ms"],
                "scanned_rows": result["scanned_rows"],
            },
        )

    def _block(self, proposal, code: str, risk: dict | None = None):
        fields = {"status": "BLOCKED", "error_code": code}
        if risk is not None:
            fields["risk_json"] = risk
        self.store.update(proposal["proposal_id"], **fields)
        self.store.audit(
            proposal["proposal_id"], "query.blocked", "worker", iso(now()),
            {"error_code": code},
        )

    def decide(
        self, proposal: dict, approve: bool, reviewer_id: str, reason: str
    ):
        if proposal["requester_id"] == reviewer_id:
            raise PermissionError("Requester cannot approve their own query")
        if proposal["status"] != "PENDING_APPROVAL":
            raise ValueError("Proposal is not awaiting approval")
        if datetime.fromisoformat(proposal["expires_at"]) <= now():
            self.store.update(proposal["proposal_id"], status="EXPIRED")
            raise ValueError("Proposal has expired")
        approval = {
            "reviewer_id": reviewer_id,
            "decision": "APPROVED" if approve else "REJECTED",
            "reason": reason,
            "decided_at": iso(now()),
            "sql_hash": proposal["sql_hash"],
        }
        changed = self.store.decide(
            proposal["proposal_id"], "PENDING_APPROVAL",
            "APPROVED" if approve else "REJECTED", approval,
        )
        if not changed:
            raise ValueError("Proposal state changed; reload and retry")
        self.store.audit(
            proposal["proposal_id"], "query.approval_decision", reviewer_id,
            approval["decided_at"], {
                "decision": approval["decision"],
                "sql_hash": proposal["sql_hash"],
            },
        )
        return self.public_view(self.store.get(proposal["proposal_id"]))

    def public_view(self, proposal: dict | None) -> dict:
        if proposal is None:
            raise KeyError("Proposal not found")
        return {
            "proposal_id": proposal["proposal_id"],
            "datasource_id": proposal["datasource_id"],
            "status": proposal["status"],
            "created_at": proposal["created_at"],
            "expires_at": proposal["expires_at"],
            "risk": proposal.get("risk"),
            "approval": proposal.get("approval"),
            "error_code": proposal.get("error_code"),
            "result_summary": ({
                "returned_rows": proposal["result"]["returned_rows"],
                "result_bytes": proposal["result"]["result_bytes"],
                "duration_ms": proposal["result"]["duration_ms"],
                "truncated": proposal["result"]["truncated"],
                "scanned_rows": proposal["result"]["scanned_rows"],
            } if proposal.get("result") else None),
        }
