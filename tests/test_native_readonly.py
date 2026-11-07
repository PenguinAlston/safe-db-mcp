from unittest.mock import MagicMock, patch
import pytest
from safe_db_mcp.adapters.factory import AdapterUnavailable, create_adapter
from safe_db_mcp.adapters.sqlalchemy_adapter import SQLAlchemyReadOnlyAdapter
from safe_db_mcp.config import DataSource

def ds(dialect, mode):
    return DataSource(datasource_id="test",name="test",dialect=dialect,url_env="TEST_DB_URL",readonly_mode=mode,allowed_tables=frozenset({"public.items"}),sensitive_columns=frozenset(),auto_execute=False,max_auto_scan_rows=0,max_hard_scan_rows=0,max_result_rows=10,max_result_bytes=10000,timeout_ms=1000,allowed_agents=frozenset({"test-agent"}))

@pytest.mark.parametrize(("dialect","mode"),[("mysql","mysql_transaction"),("postgres","postgres_transaction"),("oracle","oracle_transaction")])
def test_factory_enables_only_explicit_native_modes(dialect,mode,monkeypatch):
    monkeypatch.setenv("TEST_DB_URL","driver://placeholder")
    with patch("safe_db_mcp.adapters.factory.SQLAlchemyReadOnlyAdapter") as cls:
        create_adapter(ds(dialect,mode))
        cls.assert_called_once_with(dialect,"driver://placeholder")

def test_factory_rejects_wrong_or_unsupported_mode(monkeypatch):
    monkeypatch.setenv("TEST_DB_URL","driver://placeholder")
    with pytest.raises(AdapterUnavailable): create_adapter(ds("oracle","unsupported"))
    with pytest.raises(AdapterUnavailable): create_adapter(ds("sqlite","sqlite_readonly"))

def fake_engine(conn):
    engine=MagicMock(); ctx=MagicMock(); ctx.__enter__.return_value=conn; ctx.__exit__.return_value=False; engine.connect.return_value=ctx
    return engine

def test_oracle_verify_starts_native_readonly_before_select():
    conn=MagicMock(); conn.exec_driver_sql.return_value.scalar.return_value=1
    adapter=SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter); adapter.dialect="oracle"; adapter.engine=fake_engine(conn)
    assert adapter.verify_readonly() is True
    assert [c.args[0] for c in conn.exec_driver_sql.call_args_list]==["SET TRANSACTION READ ONLY","SELECT 1 FROM DUAL"]
    conn.rollback.assert_called_once()

def test_postgres_verify_checks_server_reported_mode():
    conn=MagicMock(); conn.exec_driver_sql.return_value.scalar.return_value="on"
    adapter=SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter); adapter.dialect="postgres"; adapter.engine=fake_engine(conn)
    assert adapter.verify_readonly() is True
    assert [c.args[0] for c in conn.exec_driver_sql.call_args_list]==["SET TRANSACTION READ ONLY","SHOW transaction_read_only"]

def test_mysql_verify_checks_server_reported_mode():
    conn=MagicMock(); conn.exec_driver_sql.return_value.scalar.return_value=1
    adapter=SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter); adapter.dialect="mysql"; adapter.engine=fake_engine(conn)
    assert adapter.verify_readonly() is True
    assert [c.args[0] for c in conn.exec_driver_sql.call_args_list]==["START TRANSACTION READ ONLY","SELECT @@transaction_read_only"]

def test_oracle_explain_is_disabled_to_avoid_plan_table_write():
    adapter=SQLAlchemyReadOnlyAdapter.__new__(SQLAlchemyReadOnlyAdapter); adapter.dialect="oracle"
    plan=adapter.explain("SELECT 1 FROM DUAL",{})
    assert plan.estimated_scan_rows is None
    assert plan.summary["planner"]=="oracle_not_run"
