import os
import uuid
from contextlib import nullcontext

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError

from safe_db_mcp.adapters.sqlalchemy_adapter import SQLAlchemyReadOnlyAdapter


@pytest.fixture(params=["postgres", "mysql"])
def live_adapter(request):
    dialect = request.param
    url = os.getenv(f"SAFEDB_TEST_{dialect.upper()}_URL")
    if not url:
        pytest.skip(f"SAFEDB_TEST_{dialect.upper()}_URL not configured")
    adapter = SQLAlchemyReadOnlyAdapter(dialect, url, 1000)
    yield adapter
    adapter.close()


def test_live_readonly_and_pool_reuse(live_adapter):
    adapter = live_adapter
    assert adapter.verify_readonly()
    for _ in range(2):
        result = adapter.execute_readonly("SELECT 1 AS id", {}, 10, 10000, 1000)
        assert result["rows"] == [{"id": 1}]
        assert not result["truncated"]
    with adapter.engine.connect() as conn:
        if adapter.dialect == "mysql":
            assert conn.exec_driver_sql("SELECT @@max_execution_time").scalar() == 0
            assert conn.exec_driver_sql(
                "SELECT @@transaction_read_only = @@global.transaction_read_only"
            ).scalar() == 1
        else:
            assert conn.exec_driver_sql("SHOW transaction_read_only").scalar() == "off"


@pytest.mark.parametrize("max_rows,max_bytes", [(1, 10000), (10, 1)])
def test_live_truncation_discards_or_resets_stream(live_adapter, max_rows, max_bytes):
    adapter = live_adapter
    response = adapter.execute_readonly(
        "SELECT 1 AS id UNION ALL SELECT 2 AS id UNION ALL SELECT 3 AS id",
        {}, max_rows, max_bytes, 1000,
    )
    assert response["truncated"]
    assert response["returned_rows"] <= max_rows
    assert response["result_bytes"] <= max_bytes
    assert adapter.execute_readonly("SELECT 1 AS id", {}, 1, 10000, 1000)["rows"] == [
        {"id": 1}
    ]


def test_live_oversized_lob_row_stops_at_byte_cap_and_releases_connection(live_adapter):
    adapter = live_adapter
    sql = (
        "SELECT repeat('x', 3000000) AS v"
        if adapter.dialect == "postgres"
        else "SELECT REPEAT('x', 3000000) AS v"
    )
    response = adapter.execute_readonly(sql, {}, 10, 1024, 2000)
    assert response["truncated"]
    assert response["result_bytes"] <= 1024
    assert response["returned_rows"] <= 1
    assert adapter.execute_readonly("SELECT 1 AS id", {}, 10, 10000, 1000)["rows"] == [
        {"id": 1}
    ]


def test_live_query_error_does_not_poison_pool(live_adapter):
    with pytest.raises(DBAPIError):
        live_adapter.execute_readonly("SELECT missing_column", {}, 10, 10000, 1000)
    assert live_adapter.verify_readonly()
    assert live_adapter.execute_readonly(
        "SELECT 1 AS id", {}, 10, 10000, 1000
    )["returned_rows"] == 1


def test_live_database_statement_timeout(live_adapter):
    sql = "SELECT pg_sleep(0.2)" if live_adapter.dialect == "postgres" else "SELECT SLEEP(0.2)"
    with pytest.raises((DBAPIError, TimeoutError)):
        live_adapter.execute_readonly(sql, {}, 1, 10000, 50)
    assert live_adapter.verify_readonly()


@pytest.fixture
def postgres_catalog_connection():
    url = os.getenv("SAFEDB_TEST_POSTGRES_ADMIN_URL")
    if not url:
        pytest.skip("SAFEDB_TEST_POSTGRES_ADMIN_URL not configured")
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as conn, conn.begin():
            schema = "safedb_test_" + uuid.uuid4().hex
            conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
            try:
                yield conn, schema
            finally:
                conn.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize("kind", ["ordinary", "inherited", "partitioned", "view"])
def test_live_postgres_catalog_rejects_implicit_objects(
    postgres_catalog_connection, monkeypatch, kind
):
    conn, schema = postgres_catalog_connection
    if kind == "view":
        conn.exec_driver_sql(f"CREATE VIEW {schema}.parent AS SELECT 1 AS id")
    else:
        suffix = " PARTITION BY RANGE(id)" if kind == "partitioned" else ""
        conn.exec_driver_sql(f"CREATE TABLE {schema}.parent (id integer){suffix}")
        if kind == "inherited":
            conn.exec_driver_sql(f"CREATE TABLE {schema}.child () INHERITS ({schema}.parent)")
            conn.exec_driver_sql(f"INSERT INTO {schema}.child VALUES (7)")
            assert conn.exec_driver_sql(f"SELECT id FROM {schema}.parent").scalar() == 7
    adapter = SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter)
    adapter.dialect = "postgres"
    monkeypatch.setattr(adapter, "_analysis_connection", lambda: nullcontext(conn))
    assert adapter.verify_allowed_tables((f"{schema}.parent",)) is (kind == "ordinary")


@pytest.mark.parametrize("phase", ["analysis", "execution"])
def test_live_postgres_builtin_functions_cannot_be_shadowed(
    postgres_catalog_connection, phase
):
    conn, schema = postgres_catalog_connection
    conn.exec_driver_sql(
        f"CREATE FUNCTION {schema}.lower(text) RETURNS text LANGUAGE SQL IMMUTABLE "
        "AS 'SELECT ''shadow'''"
    )
    conn.exec_driver_sql(f"SET LOCAL search_path = {schema}, pg_catalog")
    assert conn.exec_driver_sql("SELECT lower('X')").scalar() == "shadow"
    adapter = SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter)
    adapter.dialect = "postgres"
    adapter.timeout_ms = 1000
    if phase == "analysis":
        adapter._begin_analysis_readonly(conn)
    else:
        adapter._begin_native_readonly(conn, 1000)
    assert conn.exec_driver_sql("SELECT lower('X')").scalar() == "x"


def test_live_postgres_native_custom_column_types_are_rejected(
    postgres_catalog_connection, monkeypatch
):
    conn, schema = postgres_catalog_connection
    conn.exec_driver_sql(f"CREATE DOMAIN {schema}.custom_type AS text")
    conn.exec_driver_sql(f"CREATE TABLE {schema}.items (value {schema}.custom_type)")
    adapter = SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter)
    adapter.dialect = "postgres"
    monkeypatch.setattr(adapter, "_analysis_connection", lambda: nullcontext(conn))
    assert not adapter.verify_allowed_tables((f"{schema}.items",))


def test_live_mysql_native_readonly_rejects_write_with_write_capable_role():
    url = os.getenv("SAFEDB_TEST_MYSQL_ADMIN_URL")
    if not url:
        pytest.skip("SAFEDB_TEST_MYSQL_ADMIN_URL not configured")
    adapter = SQLAlchemyReadOnlyAdapter("mysql", url, 1000)
    table = "safedb_test_" + uuid.uuid4().hex
    try:
        with adapter.engine.connect() as conn:
            conn.exec_driver_sql(f"CREATE TABLE {table} (id integer) ENGINE=InnoDB")
            conn.commit()
            try:
                adapter._begin_native_readonly(conn, 1000)
                with pytest.raises(DBAPIError) as error:
                    conn.exec_driver_sql(f"INSERT INTO {table} VALUES (1)")
                assert error.value.orig.args[0] == 1792
            finally:
                adapter._reset_connection_controls(conn)
                conn.exec_driver_sql(f"DROP TABLE {table}")
                conn.commit()
    finally:
        adapter.close()


def test_live_native_readonly_rejects_write_with_write_capable_role(
    postgres_catalog_connection
):
    conn, schema = postgres_catalog_connection
    conn.exec_driver_sql(f"CREATE TABLE {schema}.items (id integer)")
    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
    with pytest.raises(DBAPIError) as error:
        conn.exec_driver_sql(f"INSERT INTO {schema}.items VALUES (1)")
    assert error.value.orig.sqlstate == "25006"
