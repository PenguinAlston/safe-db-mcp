from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from safe_db_mcp.config import DataSource
from safe_db_mcp.security.risk import assess_risk
from safe_db_mcp.security.sql_guard import validate_sql
from safe_db_mcp.store import ProposalStore


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.isoformat()


class QueryService:
    def __init__(
        self,
        store: ProposalStore,
        datasources: dict[str, DataSource],
        credential_provider=None,
    ):
        self.store = store
        self.datasources = datasources
        self.credential_provider = credential_provider

    def _worker_update(
        self,
        proposal: dict,
        event_type: str,
        details: dict[str, Any] | None = None,
        **fields,
    ) -> bool:
        token = proposal.get("claim_token")
        if not token:
            return False
        return self.store.update(
            proposal["proposal_id"],
            expected_claim_token=token,
            expected_status=proposal["status"],
            audit_event={
                "event_type": event_type,
                "actor_id": "worker",
                "created_at": iso(now()),
                "details": details or {},
            },
            **fields,
        )

    def prepare(
        self,
        datasource_id: str,
        sql: str,
        purpose: str,
        parameters: dict[str, Any],
        requester_id: str,
    ) -> dict:
        datasource = self.datasources.get(datasource_id)
        if datasource is None:
            raise PermissionError("Datasource is not registered")
        if requester_id not in datasource.allowed_agents:
            raise PermissionError("Agent is not authorized for this datasource")
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
        self.store.create(
            proposal,
            audit_event={
                "event_type": "query.submitted",
                "actor_id": requester_id,
                "created_at": proposal["created_at"],
                "details": {
                    "datasource_id": datasource_id,
                    "sql_hash": query.sql_hash,
                },
            },
        )
        return self.public_view(self.store.get(proposal["proposal_id"]))

    def process_analysis(self, proposal: dict) -> None:
        if datetime.fromisoformat(proposal["expires_at"]) <= now():
            self._worker_update(
                proposal, "query.expired", status="EXPIRED", error_code="PROPOSAL_EXPIRED"
            )
            return
        datasource = self.datasources.get(proposal["datasource_id"])
        if datasource is None:
            self._block(proposal, "DATASOURCE_NOT_REGISTERED")
            return
        if proposal["requester_id"] not in datasource.allowed_agents:
            self._block(proposal, "AGENT_DATASOURCE_PERMISSION_CHANGED")
            return
        query = validate_sql(proposal["sql"], datasource, proposal["parameters"])
        if query.sql_hash != proposal["sql_hash"]:
            self._block(proposal, "QUERY_HASH_MISMATCH")
            return

        from safe_db_mcp.adapters.factory import create_adapter
        adapter = (
            create_adapter(datasource)
            if self.credential_provider is None
            else create_adapter(datasource, self.credential_provider)
        )
        try:
            readonly = adapter.verify_readonly()
            if not readonly:
                self._block(proposal, "READONLY_NOT_VERIFIED")
                return
            if not adapter.verify_allowed_tables(query.tables):
                self._block(proposal, "DATABASE_OBJECT_NOT_ALLOWED")
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

            self._worker_update(
                proposal,
                "query.pending_approval",
                {
                    "risk_level": assessment.level,
                    "risk_score": assessment.score,
                },
                status="PENDING_APPROVAL",
                risk_json=assessment.model_dump(),
            )
        finally:
            adapter.close()

    def process_execution(self, proposal: dict) -> None:
        if datetime.fromisoformat(proposal["expires_at"]) <= now():
            self._worker_update(
                proposal, "query.expired", status="EXPIRED", error_code="PROPOSAL_EXPIRED"
            )
            return
        approval = proposal.get("approval") or {}
        if (
            approval.get("decision") != "APPROVED"
            or approval.get("sql_hash") != proposal["sql_hash"]
            or not proposal.get("review_token")
            or not approval.get("review_token")
            or approval["review_token"] != proposal["review_token"]
        ):
            self._block(proposal, "APPROVAL_BINDING_INVALID")
            return

        datasource = self.datasources.get(proposal["datasource_id"])
        if datasource is None:
            self._block(proposal, "DATASOURCE_NOT_REGISTERED")
            return
        if proposal["requester_id"] not in datasource.allowed_agents:
            self._block(proposal, "AGENT_DATASOURCE_PERMISSION_CHANGED")
            return
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
            if not adapter.verify_allowed_tables(query.tables):
                self._block(proposal, "DATABASE_OBJECT_NOT_ALLOWED")
                return
            plan = adapter.explain(proposal["sql"], proposal["parameters"])
            assessment = assess_risk(query, plan, datasource, True)
            if (
                plan.estimated_scan_rows is not None
                and datasource.max_hard_scan_rows > 0
                and plan.estimated_scan_rows > datasource.max_hard_scan_rows
            ):
                self._block(
                    proposal, "HARD_SCAN_LIMIT_EXCEEDED", assessment.model_dump()
                )
                return
            old = proposal.get("risk") or {}
            risk_fields = (
                "score", "level", "reasons", "estimated_scan_rows",
                "estimated_result_rows",
            )
            risk_changed = any(
                old.get(field) != getattr(assessment, field)
                for field in risk_fields
            )
            if risk_changed:
                self._worker_update(
                    proposal,
                    "query.approval_invalidated",
                    {"new_risk_level": assessment.level},
                    status="PENDING_APPROVAL",
                    risk_json=assessment.model_dump(),
                    approval_json=None,
                )
                return
            self._execute(proposal, adapter, datasource, assessment)
        finally:
            adapter.close()

    def _execute(self, proposal, adapter, datasource, assessment):
        # Planning may outlive the proposal or the claim in either execution path.
        if not self.store.owns_claim(
            proposal["proposal_id"], proposal.get("claim_token"), proposal["status"]
        ):
            return
        if datetime.fromisoformat(proposal["expires_at"]) <= now():
            self._worker_update(
                proposal, "query.expired", status="EXPIRED", error_code="PROPOSAL_EXPIRED"
            )
            return
        result = adapter.execute_readonly(
            proposal["sql"], proposal["parameters"],
            datasource.max_result_rows, datasource.max_result_bytes,
            datasource.timeout_ms,
        )
        # Completion is a compare-and-swap against this exact worker's claim.
        # A reclaimed worker cannot publish data or overwrite approval state.
        self._worker_update(
            proposal,
            "query.succeeded",
            {
                "returned_rows": result["returned_rows"],
                "result_bytes": result["result_bytes"],
                "duration_ms": result["duration_ms"],
                "scanned_rows": result["scanned_rows"],
            },
            status="SUCCEEDED",
            risk_json=assessment.model_dump(),
            result_json=result,
            error_code=None,
        )

    def _block(self, proposal, code: str, risk: dict | None = None):
        fields = {"status": "BLOCKED", "error_code": code}
        if risk is not None:
            fields["risk_json"] = risk
        self._worker_update(
            proposal,
            "query.blocked",
            {"error_code": code},
            **fields,
        )

    def decide(
        self, proposal: dict, approve: bool, reviewer_id: str, reason: str, review_token: str
    ):
        if proposal["requester_id"] == reviewer_id:
            raise PermissionError("Requester cannot approve their own query")
        if proposal["status"] != "PENDING_APPROVAL":
            raise ValueError("Proposal is not awaiting approval")
        if not review_token or review_token != proposal.get("review_token"):
            raise ValueError("Proposal review token changed; reload and retry")
        if datetime.fromisoformat(proposal["expires_at"]) <= now():
            self.store.expire_pending_approvals()
            raise ValueError("Proposal has expired")
        approval = {
            "reviewer_id": reviewer_id,
            "decision": "APPROVED" if approve else "REJECTED",
            "reason": reason,
            "decided_at": iso(now()),
            "sql_hash": proposal["sql_hash"],
            "review_token": review_token,
        }
        changed = self.store.decide(
            proposal["proposal_id"],
            "PENDING_APPROVAL",
            "APPROVED" if approve else "REJECTED",
            approval,
            audit_event={
                "event_type": "query.approval_decision",
                "actor_id": reviewer_id,
                "created_at": approval["decided_at"],
                "details": {
                    "decision": approval["decision"],
                    "sql_hash": proposal["sql_hash"],
                },
            },
        )
        if not changed:
            raise ValueError("Proposal state changed; reload and retry")
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
            "review_token": proposal.get("review_token"),
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
