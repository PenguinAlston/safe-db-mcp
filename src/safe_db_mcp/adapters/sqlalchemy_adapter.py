from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from safe_db_mcp.adapters.base import DatabaseAdapter
from safe_db_mcp.security.risk import QueryPlan


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Preserve exact decimal precision for financial and numeric data.
        return str(value)
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
    """Per-query native read-only transaction enforcement with bounded DB round-trips."""

    def __init__(self, dialect: str, database_url: str, timeout_ms: int = 5000):
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be greater than zero")
        self.dialect = dialect
        self.timeout_ms = int(timeout_ms)
        connect_timeout = max(1, math.ceil(self.timeout_ms / 1000))
        connect_args: dict[str, Any] = {}
        if dialect == "postgres":
            connect_args = {
                "connect_timeout": connect_timeout,
                "options": f"-c statement_timeout={self.timeout_ms}",
            }
        elif dialect == "mysql":
            connect_args = {
                "connect_timeout": connect_timeout,
                "read_timeout": connect_timeout,
                "write_timeout": connect_timeout,
            }
        elif dialect == "oracle":
            connect_args = {"tcp_connect_timeout": max(1, self.timeout_ms / 1000)}

        self.engine: Engine = create_engine(
            database_url,
            # Oracle must issue SET TRANSACTION READ ONLY before any SQL statement
            # in its transaction. Disable checkout ping for that dialect.
            pool_pre_ping=(dialect != "oracle"),
            pool_size=2,
            max_overflow=0,
            pool_timeout=max(0.1, self.timeout_ms / 1000),
            connect_args=connect_args,
            hide_parameters=True,
        )

    def _begin_analysis_readonly(self, conn) -> None:
        """Start a read-only metadata/planning transaction with DB-side timeouts."""
        if self.dialect == "postgres":
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(
                f"SET LOCAL statement_timeout = '{self.timeout_ms}ms'"
            )
            return
        if self.dialect == "mysql":
            conn.exec_driver_sql(
                f"SET SESSION max_execution_time = {self.timeout_ms}"
            )
            conn.exec_driver_sql("START TRANSACTION READ ONLY")
            return
        if self.dialect == "oracle":
            # call_timeout is a driver property, not a SQL statement. Set it before
            # the first transaction statement, as Oracle requires.
            conn.connection.driver_connection.call_timeout = self.timeout_ms
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            return
        raise RuntimeError(f"Unsupported analysis dialect: {self.dialect}")

    def _reset_connection_controls(self, conn) -> None:
        try:
            conn.rollback()
            if self.dialect == "mysql":
                # This is a session variable and must not leak through the pool.
                conn.exec_driver_sql("SET SESSION max_execution_time = 0")
                conn.rollback()
            elif self.dialect == "oracle":
                conn.connection.driver_connection.call_timeout = 0
        except Exception:
            conn.invalidate()

    @contextmanager
    def _analysis_connection(self) -> Iterator[Any]:
        with self.engine.connect() as conn:
            try:
                self._begin_analysis_readonly(conn)
                yield conn
            finally:
                self._reset_connection_controls(conn)

    def verify_readonly(self) -> bool:
        """Verify database-native read-only mode on a fresh, bounded connection."""
        try:
            with self._analysis_connection() as conn:
                if self.dialect == "postgres":
                    value = conn.exec_driver_sql(
                        "SHOW transaction_read_only"
                    ).scalar()
                    return str(value).lower() in {"on", "true", "1"}
                if self.dialect == "mysql":
                    value = conn.exec_driver_sql(
                        "SELECT @@transaction_read_only"
                    ).scalar()
                    return int(value) == 1
                if self.dialect == "oracle":
                    conn.exec_driver_sql("SELECT 1 FROM DUAL").scalar()
                    return True
        except (SQLAlchemyError, TypeError, ValueError, TimeoutError):
            return False
        return False

    def verify_allowed_tables(self, tables: tuple[str, ...]) -> bool:
        """Allow only exact schema-qualified base tables, never views or synonyms."""
        if not tables:
            return False
        try:
            with self._analysis_connection() as conn:
                for key in tables:
                    parts = key.split(".")
                    if len(parts) != 2 or not all(parts):
                        return False
                    schema, table = parts
                    if self.dialect == "mysql":
                        row = conn.execute(
                            text(
                                "SELECT TABLE_TYPE FROM information_schema.TABLES "
                                "WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table"
                            ),
                            {"schema": schema, "table": table},
                        ).scalar()
                        if row != "BASE TABLE":
                            return False
                    elif self.dialect == "postgres":
                        row = conn.execute(
                            text(
                                "SELECT c.relkind FROM pg_class c "
                                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                                "WHERE n.nspname=:schema AND c.relname=:table"
                            ),
                            {"schema": schema, "table": table},
                        ).scalar()
                        if row not in {"r", "p"}:
                            return False
                    elif self.dialect == "oracle":
                        row = conn.execute(
                            text(
                                "SELECT OBJECT_TYPE FROM ALL_OBJECTS "
                                "WHERE OWNER=:schema AND OBJECT_NAME=:table "
                                "AND OBJECT_TYPE='TABLE' "
                                "AND NOT EXISTS ("
                                "SELECT 1 FROM ALL_EXTERNAL_TABLES e "
                                "WHERE e.OWNER=ALL_OBJECTS.OWNER "
                                "AND e.TABLE_NAME=ALL_OBJECTS.OBJECT_NAME)"
                            ),
                            {"schema": schema.upper(), "table": table.upper()},
                        ).scalar()
                        if row != "TABLE":
                            return False
                    else:
                        return False
            return True
        except (SQLAlchemyError, TimeoutError):
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

        with self._analysis_connection() as conn:
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
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be greater than zero")
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
            # Set a driver-level bound before the first transaction statement.
            conn.connection.driver_connection.call_timeout = int(timeout_ms)
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
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
        if timeout_ms <= 0 or max_rows <= 0 or max_bytes <= 0:
            raise ValueError("timeout_ms, max_rows and max_bytes must be positive")
        started = time.monotonic()
        deadline = started + timeout_ms / 1000
        rows_out = []
        byte_count = 0
        truncated = False
        failed = False

        with self.engine.connect() as conn:
            try:
                self._begin_native_readonly(conn, timeout_ms)
                if self.dialect == "oracle":
                    driver = conn.connection.driver_connection
                    driver.call_timeout = min(
                        timeout_ms, max(1, int((deadline - time.monotonic()) * 1000))
                    )
                result = conn.execution_options(stream_results=True).execute(
                    text(sql), parameters
                )
                columns = list(result.keys())
                if len(columns) != len(set(columns)):
                    raise RuntimeError(
                        "Duplicate result column names are not supported"
                    )

                while len(rows_out) < max_rows + 1:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if self.dialect == "oracle":
                            conn.connection.driver_connection.cancel()
                        raise TimeoutError("Query exceeded total execution time budget")
                    if self.dialect == "oracle":
                        conn.connection.driver_connection.call_timeout = min(
                            timeout_ms, max(1, int(remaining * 1000))
                        )
                    # One-row streaming reduces batch buffering before the byte cap.
                    batch = result.fetchmany(1)
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
            except BaseException:
                failed = True
                raise
            finally:
                if self.dialect == "mysql" and (failed or truncated):
                    # PyMySQL's unbuffered cursor close can drain all remaining rows.
                    # Destroy the DBAPI connection instead of returning a live stream
                    # to the pool or performing a potentially unbounded drain.
                    conn.invalidate()
                else:
                    self._reset_connection_controls(conn)

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
