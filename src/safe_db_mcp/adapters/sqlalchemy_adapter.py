from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator

from pymysql.connections import Connection as PyMySQLConnection
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
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _walk_postgres(plan: dict) -> tuple[int | None, int | None]:
    scan_types = {
        "Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
        "Bitmap Index Scan", "Tid Scan", "Tid Range Scan",
    }
    container_types = {
        "Limit", "Aggregate", "Group", "Sort", "Incremental Sort", "Unique",
        "Hash", "Hash Join", "Merge Join", "Append", "Merge Append", "Result",
        "Materialize", "Subquery Scan", "BitmapAnd", "BitmapOr", "SetOp",
    }
    total = 0
    found = False
    reliable = True

    def walk(node):
        nonlocal total, found, reliable
        if not isinstance(node, dict):
            reliable = False
            return
        kind = node.get("Node Type")
        if (
            kind not in scan_types | container_types
            or node.get("Parent Relationship") in {"SubPlan", "InitPlan"}
            or node.get("Parallel Aware")
        ):
            reliable = False
        if kind in scan_types:
            rows = node.get("Plan Rows")
            # Plan Rows counts survivors, not tuples inspected before a scan filter.
            if node.get("Filter") or node.get("Recheck Cond") or type(rows) is not int:
                reliable = False
            elif rows < 0:
                reliable = False
            else:
                total += rows
                found = True
        for child in node.get("Plans", []):
            walk(child)

    walk(plan)
    returned = plan.get("Plan Rows")
    return (
        total if found and reliable else None,
        returned if type(returned) is int and returned >= 0 else None,
    )


def _walk_mysql(value: Any) -> int | None:
    found = []

    def walk(item):
        if isinstance(item, dict):
            rows = item.get("rows_examined_per_scan")
            if rows is None and "table_name" in item:
                rows = item.get("rows")
            if type(rows) in {int, float} and math.isfinite(rows) and rows >= 0:
                found.append(math.ceil(rows))
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    # Per-scan counts do not account for repeated scans in joins or subqueries.
    return found[0] if len(found) == 1 else None


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
            conn.exec_driver_sql("SET LOCAL search_path = pg_catalog")
            conn.exec_driver_sql(
                f"SET LOCAL statement_timeout = '{self.timeout_ms}ms'"
            )
            return
        if self.dialect == "mysql":
            conn.exec_driver_sql(
                f"SET SESSION max_execution_time = {self.timeout_ms}"
            )
            # MySQL reports the session default, not a READ ONLY transaction override.
            conn.exec_driver_sql("SET SESSION transaction_read_only = ON")
            conn.exec_driver_sql("START TRANSACTION")
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
                # These are session variables and must not leak through the pool.
                conn.exec_driver_sql("SET SESSION max_execution_time = 0")
                conn.exec_driver_sql("SET SESSION transaction_read_only = DEFAULT")
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
                                "SELECT TABLE_TYPE, ENGINE FROM information_schema.TABLES "
                                "WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table"
                            ),
                            {"schema": schema, "table": table},
                        ).one_or_none()
                        if row != ("BASE TABLE", "InnoDB"):
                            return False
                    elif self.dialect == "postgres":
                        row = conn.execute(
                            text(
                                "SELECT c.relkind FROM pg_catalog.pg_class c "
                                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                                "WHERE n.nspname=:schema AND c.relname=:table "
                                "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i "
                                "WHERE i.inhparent=c.oid) "
                                "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a "
                                "JOIN pg_catalog.pg_type t ON t.oid=a.atttypid "
                                "JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace "
                                "WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped "
                                "AND (t.typtype<>'b' OR tn.nspname<>'pg_catalog'))"
                            ),
                            {"schema": schema, "table": table},
                        ).scalar()
                        if row != "r":
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
            conn.exec_driver_sql("SET LOCAL search_path = pg_catalog")
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
            # Set the session default so the reported and effective modes agree.
            conn.exec_driver_sql("SET SESSION transaction_read_only = ON")
            conn.exec_driver_sql("START TRANSACTION")
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
        result = None

        with self.engine.connect() as conn:
            mysql_connection = (
                conn.connection.driver_connection if self.dialect == "mysql" else None
            )
            try:
                self._begin_native_readonly(conn, timeout_ms)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Query exceeded total execution time budget")
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
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Query exceeded total execution time budget")
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
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Query exceeded total execution time budget")
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
                    conn.invalidate()
                    # Stop PyMySQL finalizers draining a stream after its socket is closed.
                    if isinstance(mysql_connection, PyMySQLConnection):
                        stream = mysql_connection._result
                        if stream is not None:
                            stream.unbuffered_active = False
                            stream.connection = None
                    if result is not None:
                        result.close()
                else:
                    try:
                        if result is not None:
                            result.close()
                    finally:
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
