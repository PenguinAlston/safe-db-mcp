from __future__ import annotations

import json
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from safe_db_mcp.adapters.base import DatabaseAdapter
from safe_db_mcp.security.risk import QueryPlan


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _walk_postgres(plan: dict) -> tuple[int | None, int | None]:
    scan_types = {
        "Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
        "Bitmap Index Scan", "Foreign Scan", "Tid Scan",
    }
    scan_total = 0
    found_scan = False

    def walk(node):
        nonlocal scan_total, found_scan
        if not isinstance(node, dict):
            return
        if node.get("Node Type") in scan_types and isinstance(node.get("Plan Rows"), int):
            scan_total += node["Plan Rows"]
            found_scan = True
        for child in node.get("Plans", []):
            walk(child)

    walk(plan)
    result_rows = plan.get("Plan Rows")
    return (
        scan_total if found_scan else None,
        result_rows if isinstance(result_rows, int) else None,
    )


def _walk_mysql(value: Any) -> int | None:
    found = []

    def walk(item):
        if isinstance(item, dict):
            rows = item.get("rows_examined_per_scan")
            if rows is None and "table_name" in item:
                rows = item.get("rows")
            if isinstance(rows, (int, float)):
                found.append(int(rows))
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return sum(found) if found else None


class SQLAlchemyReadOnlyAdapter(DatabaseAdapter):
    def __init__(self, dialect: str, database_url: str):
        self.dialect = dialect
        self.engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=0,
            hide_parameters=True,
        )

    def verify_readonly(self) -> bool:
        try:
            with self.engine.connect() as conn:
                if self.dialect == "postgres":
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    value = conn.execute(text("SHOW transaction_read_only")).scalar()
                    conn.rollback()
                    return str(value).lower() in {"on", "true", "1"}
                if self.dialect == "mysql":
                    conn.exec_driver_sql("START TRANSACTION READ ONLY")
                    value = conn.execute(
                        text("SELECT @@transaction_read_only")
                    ).scalar()
                    conn.rollback()
                    return int(value) == 1
        except Exception:
            return False
        return False

    def explain(self, sql: str, parameters: dict[str, Any]) -> QueryPlan:
        with self.engine.connect() as conn:
            if self.dialect == "postgres":
                raw = conn.execute(
                    text("EXPLAIN (FORMAT JSON) " + sql), parameters
                ).scalar()
                payload = raw[0] if isinstance(raw, list) else raw
                plan = payload.get("Plan", {}) if isinstance(payload, dict) else {}
                scanned, returned = _walk_postgres(plan)
                return QueryPlan(scanned, returned, {"planner": "postgres_explain"})
            if self.dialect == "mysql":
                raw = conn.execute(
                    text("EXPLAIN FORMAT=JSON " + sql), parameters
                ).scalar()
                payload = json.loads(raw) if isinstance(raw, str) else raw
                scanned = _walk_mysql(payload)
                return QueryPlan(scanned, None, {"planner": "mysql_explain_json"})
        raise RuntimeError("EXPLAIN is unsupported for this dialect")

    def execute_readonly(
        self, sql: str, parameters: dict[str, Any],
        max_rows: int, max_bytes: int, timeout_ms: int
    ) -> dict[str, Any]:
        started = time.monotonic()
        rows_out = []
        byte_count = 0
        truncated = False

        with self.engine.connect() as conn:
            try:
                if self.dialect == "postgres":
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    mode = conn.execute(text("SHOW transaction_read_only")).scalar()
                    if str(mode).lower() not in {"on", "true", "1"}:
                        raise RuntimeError("Native read-only transaction was not enabled")
                    conn.exec_driver_sql(
                        f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'"
                    )
                elif self.dialect == "mysql":
                    conn.exec_driver_sql(
                        f"SET SESSION max_execution_time = {int(timeout_ms)}"
                    )
                    conn.exec_driver_sql("START TRANSACTION READ ONLY")
                    mode = conn.execute(
                        text("SELECT @@transaction_read_only")
                    ).scalar()
                    if int(mode) != 1:
                        raise RuntimeError("Native read-only transaction was not enabled")
                else:
                    raise RuntimeError("Unsupported read-only dialect")

                result = conn.execute(text(sql), parameters)
                columns = list(result.keys())
                while len(rows_out) < max_rows + 1:
                    batch = result.fetchmany(min(100, max_rows + 1 - len(rows_out)))
                    if not batch:
                        break
                    for row in batch:
                        item = {
                            key: _json_value(value)
                            for key, value in zip(columns, row)
                        }
                        encoded = json.dumps(
                            item, ensure_ascii=False, default=str
                        ).encode("utf-8")
                        if len(rows_out) >= max_rows or byte_count + len(encoded) > max_bytes:
                            truncated = True
                            break
                        rows_out.append(item)
                        byte_count += len(encoded)
                    if truncated:
                        break
            finally:
                conn.rollback()
                if self.dialect == "mysql":
                    try:
                        conn.exec_driver_sql("SET SESSION max_execution_time = 0")
                    except Exception:
                        pass

        return {
            "rows": rows_out,
            "returned_rows": len(rows_out),
            "result_bytes": byte_count,
            "truncated": truncated,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "scanned_rows": None,
            "scan_metric_note": (
                "Exact runtime scanned rows are unavailable from this adapter."
            ),
        }

    def close(self):
        self.engine.dispose()
