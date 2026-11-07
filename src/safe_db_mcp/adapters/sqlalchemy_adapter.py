from __future__ import annotations

import json
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

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
    total = 0
    found = False

    def walk(node):
        nonlocal total, found
        if not isinstance(node, dict):
            return
        if node.get("Node Type") in scan_types and isinstance(node.get("Plan Rows"), int):
            total += node["Plan Rows"]
            found = True
        for child in node.get("Plans", []):
            walk(child)

    walk(plan)
    returned = plan.get("Plan Rows")
    return total if found else None, returned if isinstance(returned, int) else None


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
    """Per-query native read-only transaction enforcement."""

    def __init__(self, dialect: str, database_url: str):
        self.dialect = dialect
        self.engine: Engine = create_engine(
            database_url,
            # Oracle must issue SET TRANSACTION READ ONLY before any statement
            # in its transaction. Disable checkout ping for that dialect.
            pool_pre_ping=(dialect != "oracle"),
            pool_size=2,
            max_overflow=0,
            hide_parameters=True,
        )

    def verify_readonly(self) -> bool:
        """Check native read-only transaction activation on a fresh connection.

        Oracle has no transaction_read_only status variable. For Oracle, successful
        SET TRANSACTION READ ONLY followed by a harmless SELECT is the capability
        check; the same native command is repeated before every user query.
        """
        try:
            with self.engine.connect() as conn:
                if self.dialect == "postgres":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    value = conn.exec_driver_sql(
                        "SHOW transaction_read_only"
                    ).scalar()
                    conn.rollback()
                    return str(value).lower() in {"on", "true", "1"}
                if self.dialect == "mysql":
                    conn.exec_driver_sql("START TRANSACTION READ ONLY")
                    value = conn.exec_driver_sql(
                        "SELECT @@transaction_read_only"
                    ).scalar()
                    conn.rollback()
                    return int(value) == 1
                if self.dialect == "oracle":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    conn.exec_driver_sql("SELECT 1 FROM DUAL").scalar()
                    conn.rollback()
                    return True
        except (SQLAlchemyError, TypeError, ValueError):
            return False
        return False

    def explain(self, sql: str, parameters: dict[str, Any]) -> QueryPlan:
        if self.dialect == "oracle":
            # Oracle EXPLAIN PLAN writes PLAN_TABLE. Avoid hidden writes here;
            # unknown estimates force approval and keep auto-execution disabled.
            return QueryPlan(
                None,
                None,
                {
                    "planner": "oracle_not_run",
                    "reason": "EXPLAIN PLAN writes PLAN_TABLE",
                },
            )

        with self.engine.connect() as conn:
            if self.dialect == "postgres":
                raw = conn.execute(
                    text("EXPLAIN (FORMAT JSON) " + sql), parameters
                ).scalar()
                payload = raw[0] if isinstance(raw, list) else raw
                plan = payload.get("Plan", {}) if isinstance(payload, dict) else {}
                scanned, returned = _walk_postgres(plan)
                return QueryPlan(
                    scanned, returned, {"planner": "postgres_explain"}
                )
            if self.dialect == "mysql":
                raw = conn.execute(
                    text("EXPLAIN FORMAT=JSON " + sql), parameters
                ).scalar()
                payload = json.loads(raw) if isinstance(raw, str) else raw
                return QueryPlan(
                    _walk_mysql(payload), None, {"planner": "mysql_explain_json"}
                )
        raise RuntimeError("EXPLAIN is unsupported for this dialect")

    def _begin_native_readonly(self, conn, timeout_ms: int) -> None:
        if self.dialect == "postgres":
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            mode = conn.exec_driver_sql(
                "SHOW transaction_read_only"
            ).scalar()
            if str(mode).lower() not in {"on", "true", "1"}:
                raise RuntimeError(
                    "PostgreSQL native read-only transaction not enabled"
                )
            conn.exec_driver_sql(
                f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'"
            )
            return

        if self.dialect == "mysql":
            conn.exec_driver_sql(
                f"SET SESSION max_execution_time = {int(timeout_ms)}"
            )
            conn.exec_driver_sql("START TRANSACTION READ ONLY")
            mode = conn.exec_driver_sql(
                "SELECT @@transaction_read_only"
            ).scalar()
            if int(mode) != 1:
                raise RuntimeError("MySQL native read-only transaction not enabled")
            return

        if self.dialect == "oracle":
            # This must be the first statement in the Oracle transaction.
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            # python-oracledb call_timeout is per round-trip and measured in ms.
            conn.connection.driver_connection.call_timeout = int(timeout_ms)
            return

        raise RuntimeError(f"Unsupported read-only dialect: {self.dialect}")

    def execute_readonly(
        self,
        sql: str,
        parameters: dict[str, Any],
        max_rows: int,
        max_bytes: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        started = time.monotonic()
        rows_out = []
        byte_count = 0
        truncated = False

        with self.engine.connect() as conn:
            try:
                self._begin_native_readonly(conn, timeout_ms)
                result = conn.execute(text(sql), parameters)
                columns = list(result.keys())
                while len(rows_out) < max_rows + 1:
                    batch = result.fetchmany(
                        min(100, max_rows + 1 - len(rows_out))
                    )
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
                        if (
                            len(rows_out) >= max_rows
                            or byte_count + len(encoded) > max_bytes
                        ):
                            truncated = True
                            break
                        rows_out.append(item)
                        byte_count += len(encoded)
                    if truncated:
                        break
            finally:
                try:
                    conn.rollback()
                finally:
                    if self.dialect == "mysql":
                        try:
                            conn.exec_driver_sql(
                                "SET SESSION max_execution_time = 0"
                            )
                        except SQLAlchemyError:
                            conn.invalidate()
                    elif self.dialect == "oracle":
                        try:
                            conn.connection.driver_connection.call_timeout = 0
                        except Exception:
                            conn.invalidate()

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
