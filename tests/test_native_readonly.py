from unittest.mock import MagicMock, patch

import pytest

from safe_db_mcp.adapters.factory import AdapterUnavailable, create_adapter
from safe_db_mcp.adapters.sqlalchemy_adapter import SQLAlchemyReadOnlyAdapter
from safe_db_mcp.config import DataSource


def make_datasource(dialect: str, mode: str) -> DataSource:
    return DataSource(
        datasource_id="test",
        name="test",
        dialect=dialect,
        url_env="TEST_DB_URL",
        readonly_mode=mode,
        allowed_tables=frozenset({"public.items"}),
        sensitive_columns=frozenset(),
        auto_execute=False,
        max_auto_scan_rows=0,
        max_hard_scan_rows=0,
        max_result_rows=10,
        max_result_bytes=10000,
        timeout_ms=1000,
        allowed_agents=frozenset({"test-agent"}),
    )


@pytest.mark.parametrize(
    ("dialect", "mode"),
    [
        ("mysql", "mysql_transaction"),
        ("postgres", "postgres_transaction"),
        ("oracle", "oracle_transaction"),
    ],
)
def test_factory_enables_only_explicit_native_modes(dialect, mode, monkeypatch):
    monkeypatch.setenv("TEST_DB_URL", "driver://placeholder")
    with patch(
        "safe_db_mcp.adapters.factory.SQLAlchemyReadOnlyAdapter"
    ) as adapter_class:
        create_adapter(make_datasource(dialect, mode))
        adapter_class.assert_called_once_with(
            dialect, "driver://placeholder", 1000
        )


def test_factory_rejects_wrong_or_unsupported_mode(monkeypatch):
    monkeypatch.setenv("TEST_DB_URL", "driver://placeholder")
    with pytest.raises(AdapterUnavailable):
        create_adapter(make_datasource("oracle", "unsupported"))
    with pytest.raises(AdapterUnavailable):
        create_adapter(make_datasource("sqlite", "sqlite_readonly"))


def make_fake_engine(connection):
    engine = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection
    context.__exit__.return_value = False
    engine.connect.return_value = context
    return engine


def make_adapter(dialect: str, connection):
    adapter = SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter)
    adapter.dialect = dialect
    adapter.timeout_ms = 1000
    adapter.engine = make_fake_engine(connection)
    return adapter


def test_oracle_verify_starts_native_readonly_before_select():
    connection = MagicMock()
    connection.exec_driver_sql.return_value.scalar.return_value = 1
    adapter = make_adapter("oracle", connection)

    assert adapter.verify_readonly() is True
    statements = [
        call.args[0] for call in connection.exec_driver_sql.call_args_list
    ]
    assert statements == [
        "SET TRANSACTION READ ONLY",
        "SELECT 1 FROM DUAL",
    ]
    connection.rollback.assert_called_once()
    assert connection.connection.driver_connection.call_timeout == 0


def test_postgres_verify_checks_server_reported_mode_and_statement_timeout():
    connection = MagicMock()
    connection.exec_driver_sql.return_value.scalar.return_value = "on"
    adapter = make_adapter("postgres", connection)

    assert adapter.verify_readonly() is True
    statements = [
        call.args[0] for call in connection.exec_driver_sql.call_args_list
    ]
    assert statements == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL search_path = pg_catalog",
        "SET LOCAL statement_timeout = '1000ms'",
        "SHOW transaction_read_only",
    ]
    connection.rollback.assert_called_once()


def test_mysql_verify_checks_mode_and_resets_session_timeout():
    connection = MagicMock()
    connection.exec_driver_sql.return_value.scalar.return_value = 1
    adapter = make_adapter("mysql", connection)

    assert adapter.verify_readonly() is True
    statements = [
        call.args[0] for call in connection.exec_driver_sql.call_args_list
    ]
    assert statements == [
        "SET SESSION max_execution_time = 1000",
        "SET SESSION transaction_read_only = ON",
        "START TRANSACTION",
        "SELECT @@transaction_read_only",
        "SET SESSION max_execution_time = 0",
        "SET SESSION transaction_read_only = DEFAULT",
    ]
    assert connection.rollback.call_count == 2


def test_mysql_verify_fails_closed_when_server_reports_writable():
    connection = MagicMock()
    connection.exec_driver_sql.return_value.scalar.return_value = 0
    adapter = make_adapter("mysql", connection)

    assert adapter.verify_readonly() is False


def test_oracle_explain_is_disabled_to_avoid_plan_table_write():
    adapter = SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter)
    adapter.dialect = "oracle"

    plan = adapter.explain("SELECT 1 FROM DUAL", {})

    assert plan.estimated_scan_rows is None
    assert plan.summary["planner"] == "oracle_not_run"


def test_decimal_serialization_preserves_precision():
    from decimal import Decimal

    from safe_db_mcp.adapters.sqlalchemy_adapter import _json_value

    value = Decimal("1234567890.12345678901234567890")
    assert _json_value(value) == "1234567890.12345678901234567890"


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("mysql", "BASE TABLE"),
        ("postgres", "r"),
        ("oracle", "TABLE"),
    ],
)
def test_catalog_check_accepts_base_tables(dialect, expected):
    connection = MagicMock()
    connection.execute.return_value.scalar.return_value = expected
    connection.execute.return_value.one_or_none.return_value = (expected, "InnoDB")
    adapter = make_adapter(dialect, connection)

    assert adapter.verify_allowed_tables(("public.items",)) is True
    connection.execute.assert_called_once()


@pytest.mark.parametrize("dialect", ["mysql", "postgres", "oracle"])
def test_catalog_check_rejects_non_base_objects(dialect):
    connection = MagicMock()
    connection.execute.return_value.scalar.return_value = "VIEW"
    adapter = make_adapter(dialect, connection)

    assert adapter.verify_allowed_tables(("public.items",)) is False


@pytest.mark.parametrize("engine", ["FEDERATED", "MyISAM", None])
def test_mysql_catalog_rejects_unverified_storage_engines(engine):
    connection = MagicMock()
    connection.execute.return_value.scalar.return_value = "BASE TABLE"
    connection.execute.return_value.one_or_none.return_value = ("BASE TABLE", engine)
    adapter = make_adapter("mysql", connection)
    assert adapter.verify_allowed_tables(("public.items",)) is False


def test_mysql_truncated_stream_invalidates_connection_without_rollback():
    connection = MagicMock()
    result = MagicMock()
    result.keys.return_value = ["id"]
    result.fetchmany.side_effect = [[(1,)], [(2,)]]
    connection.execution_options.return_value.execute.return_value = result
    adapter = make_adapter("mysql", connection)
    adapter._begin_native_readonly = MagicMock()

    response = adapter.execute_readonly(
        "SELECT id FROM sales.orders LIMIT 2", {}, 1, 10000, 1000
    )

    assert response["truncated"] is True
    assert response["rows"] == [{"id": 1}]
    connection.invalidate.assert_called_once()
    connection.rollback.assert_not_called()


def test_duplicate_result_column_names_fail_closed():
    connection = MagicMock()
    result = MagicMock()
    result.keys.return_value = ["id", "id"]
    connection.execution_options.return_value.execute.return_value = result
    adapter = make_adapter("mysql", connection)
    adapter._begin_native_readonly = MagicMock()

    with pytest.raises(RuntimeError, match="Duplicate result column names"):
        adapter.execute_readonly(
            "SELECT id, id FROM sales.orders LIMIT 1", {}, 10, 10000, 1000
        )
    connection.invalidate.assert_called_once()


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle"])
@pytest.mark.parametrize("batch", [[], [(1,)]])
def test_final_fetch_cannot_publish_after_deadline(dialect, batch):
    connection = MagicMock()
    result = MagicMock()
    result.keys.return_value = ["id"]
    clock = [0.0]

    def fetch(_size):
        clock[0] = 2.0
        return batch

    result.fetchmany.side_effect = fetch
    connection.execution_options.return_value.execute.return_value = result
    adapter = make_adapter(dialect, connection)
    adapter._begin_native_readonly = MagicMock()
    with patch(
        "safe_db_mcp.adapters.sqlalchemy_adapter.time.monotonic",
        side_effect=lambda: clock[0],
    ), pytest.raises(TimeoutError):
        adapter.execute_readonly("SELECT id FROM public.items", {}, 1, 1, 1000)


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle"])
def test_expired_setup_budget_does_not_issue_user_query(dialect):
    connection = MagicMock()
    adapter = make_adapter(dialect, connection)
    clock = [0.0]

    def begin(*_args):
        clock[0] = 2.0

    adapter._begin_native_readonly = begin
    with patch(
        "safe_db_mcp.adapters.sqlalchemy_adapter.time.monotonic",
        side_effect=lambda: clock[0],
    ), pytest.raises(TimeoutError):
        adapter.execute_readonly("SELECT id FROM public.items", {}, 1, 10000, 1000)
    connection.execution_options.assert_not_called()


def test_mysql_discard_deactivates_real_pymysql_stream():
    from pymysql.connections import Connection, MySQLResult
    from pymysql.cursors import SSCursor

    raw = Connection(defer_connect=True)
    stream = MySQLResult(raw)
    stream.unbuffered_active = True
    raw._result = stream
    cursor = SSCursor(raw)
    cursor._result = stream
    result = MagicMock()
    result.cursor = cursor
    result.close.side_effect = cursor.close
    result.keys.return_value = ["id"]
    result.fetchmany.side_effect = [[(1,)], [(2,)]]
    connection = MagicMock()
    connection.connection.driver_connection = raw
    connection.execution_options.return_value.execute.return_value = result
    adapter = make_adapter("mysql", connection)
    adapter._begin_native_readonly = MagicMock()
    try:
        adapter.execute_readonly("SELECT 1", {}, 1, 10000, 1000)
        assert stream.unbuffered_active is False
        result.close.assert_called_once()
        connection.invalidate.assert_called_once()
        connection.rollback.assert_not_called()
    finally:
        stream.unbuffered_active = False
        cursor.close()


@pytest.mark.parametrize("plan", [
    {"Node Type": "Seq Scan", "Plan Rows": 1, "Filter": "id = 1"},
    {"Node Type": "Index Scan", "Plan Rows": 1, "Filter": "amount > 10"},
    {"Node Type": "Nested Loop", "Plan Rows": 100000, "Plans": [
        {"Node Type": "Seq Scan", "Plan Rows": 1000},
        {"Node Type": "Index Scan", "Plan Rows": 100},
    ]},
    {"Node Type": "Append", "Plan Rows": 100, "Plans": [
        {"Node Type": "Seq Scan", "Plan Rows": 1},
        {"Node Type": "Custom Scan", "Plan Rows": 99},
    ]},
])
def test_postgres_unreliable_scan_counts_are_unknown(plan):
    from safe_db_mcp.adapters.sqlalchemy_adapter import _walk_postgres

    assert _walk_postgres(plan)[0] is None


def test_mysql_repeated_join_scans_are_unknown():
    from safe_db_mcp.adapters.sqlalchemy_adapter import _walk_mysql

    plan = {"query_block": {"nested_loop": [
        {"table": {"table_name": "a", "rows_examined_per_scan": 1000}},
        {"table": {"table_name": "b", "rows_examined_per_scan": 100}},
    ]}}
    assert _walk_mysql(plan) is None


@pytest.mark.parametrize("value,expected", [
    (float("nan"), "nan"),
    (float("inf"), "inf"),
    (float("-inf"), "-inf"),
    ([float("nan"), [float("inf")]], ["nan", ["inf"]]),
])
def test_nonfinite_result_values_remain_json_safe(value, expected):
    import json

    from safe_db_mcp.adapters.sqlalchemy_adapter import _json_value

    normalized = _json_value(value)
    assert normalized == expected
    json.dumps(normalized, allow_nan=False)


def test_simple_unfiltered_scan_estimates_remain_available():
    from safe_db_mcp.adapters.sqlalchemy_adapter import _walk_mysql, _walk_postgres

    assert _walk_postgres({"Node Type": "Seq Scan", "Plan Rows": 3}) == (3, 3)
    assert _walk_mysql({"table_name": "a", "rows_examined_per_scan": 3}) == 3


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle"])
def test_reset_failure_discards_connection(dialect):
    connection = MagicMock()
    connection.rollback.side_effect = RuntimeError("reset failed")
    adapter = make_adapter(dialect, connection)
    adapter._reset_connection_controls(connection)
    connection.invalidate.assert_called_once()
