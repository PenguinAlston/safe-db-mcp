from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Principal(BaseModel):
    actor_id: str
    role: Literal["agent", "reviewer", "worker"]


class PrepareQueryRequest(BaseModel):
    datasource_id: str = Field(min_length=1, max_length=64)
    sql: str = Field(min_length=1, max_length=50000)
    purpose: str = Field(min_length=3, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    approve: bool
    reason: str = Field(min_length=3, max_length=1000)


class RiskAssessment(BaseModel):
    score: int
    level: Literal["LOW", "MEDIUM", "HIGH"]
    reasons: list[str]
    estimated_scan_rows: int | None = None
    estimated_result_rows: int | None = None
    read_only_verified: bool
    auto_eligible: bool = False
