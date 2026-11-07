from __future__ import annotations

from dataclasses import dataclass

from safe_db_mcp.config import DataSource
from safe_db_mcp.models import RiskAssessment
from safe_db_mcp.security.sql_guard import ValidatedQuery


@dataclass(frozen=True)
class QueryPlan:
    estimated_scan_rows: int | None
    estimated_result_rows: int | None
    summary: dict


def assess_risk(
    query: ValidatedQuery,
    plan: QueryPlan,
    datasource: DataSource,
    read_only_verified: bool,
) -> RiskAssessment:
    score = 0
    reasons: list[str] = []

    rows = plan.estimated_scan_rows
    if rows is None:
        score += 20
        reasons.append("SCAN_ESTIMATE_UNAVAILABLE")
    elif rows > 100_000:
        score += 30
        reasons.append("LARGE_SCAN_ESTIMATE")
    elif rows > 10_000:
        score += 15
        reasons.append("MODERATE_SCAN_ESTIMATE")

    if plan.estimated_result_rows is None:
        score += 5
        reasons.append("RESULT_ESTIMATE_UNAVAILABLE")
    elif plan.estimated_result_rows > 1000:
        score += 20
        reasons.append("LARGE_RESULT_ESTIMATE")
    elif plan.estimated_result_rows > 100:
        score += 10
        reasons.append("MODERATE_RESULT_ESTIMATE")

    if query.join_count >= 3:
        score += 15
        reasons.append("MULTIPLE_JOINS")
    elif query.join_count:
        score += 5
        reasons.append("JOIN_QUERY")

    if query.select_count > 3:
        score += 10
        reasons.append("DEEP_OR_COMPLEX_QUERY")

    if not query.has_top_level_limit:
        score += 15
        reasons.append("NO_TOP_LEVEL_LIMIT")
    elif query.limit_value is None:
        score += 15
        reasons.append("NON_LITERAL_LIMIT")
    elif query.limit_value > datasource.max_result_rows:
        score += 15
        reasons.append("LIMIT_EXCEEDS_RESULT_CAP")

    score = min(score, 100)
    level = "LOW" if score < 20 else "MEDIUM" if score < 50 else "HIGH"

    # Use short-circuit operators so absent estimates/limits fail closed.
    auto_eligible = (
        level == "LOW"
        and datasource.auto_execute
        and read_only_verified
        and rows is not None
        and rows <= datasource.max_auto_scan_rows
        and query.limit_value is not None
        and 0 < query.limit_value <= datasource.max_result_rows
        and not (set(query.columns) & datasource.sensitive_columns)
    )

    return RiskAssessment(
        score=score,
        level=level,
        reasons=reasons,
        estimated_scan_rows=rows,
        estimated_result_rows=plan.estimated_result_rows,
        read_only_verified=read_only_verified,
        auto_eligible=auto_eligible,
    )
