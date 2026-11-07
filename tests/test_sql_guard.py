import pytest

from safe_db_mcp.config import DataSource
from safe_db_mcp.security.sql_guard import SQLRejected, validate_sql


@pytest.fixture
def datasource():
    return DataSource(
        datasource_id="db",
        name="test",
        dialect="postgres",
        url_env="DB_URL",
        readonly_mode="postgres_transaction",
        allowed_tables=frozenset({"public.orders", "public.customers"}),
        sensitive_columns=frozenset({"password_hash", "email"}),
        auto_execute=True,
        max_auto_scan_rows=1000,
        max_hard_scan_rows=10000,
        max_result_rows=100,
        max_result_bytes=100000,
        timeout_ms=1000,
    )


def test_select_from_allowed_table(datasource):
    result = validate_sql(
        "SELECT id FROM public.orders LIMIT 10", datasource, {}
    )
    assert result.tables == ("public.orders",)
    assert result.has_top_level_limit


@pytest.mark.parametrize("sql", [
    "DELETE FROM public.orders",
    "UPDATE public.orders SET id = 1",
    "INSERT INTO public.orders(id) VALUES (1)",
    "DROP TABLE public.orders",
    "SELECT id INTO new_table FROM public.orders",
    "SELECT id FROM public.orders FOR UPDATE",
    "SELECT * FROM public.orders",
    "SELECT id FROM public.other_table",
    "SELECT id FROM public.orders; SELECT id FROM public.customers",
    "WITH x AS (DELETE FROM public.orders RETURNING id) SELECT id FROM x",
    "WITH orders AS (SELECT id FROM private.orders) SELECT id FROM orders",
])
def test_unsafe_sql_is_rejected(datasource, sql):
    with pytest.raises(SQLRejected):
        validate_sql(sql, datasource, {})


def test_sensitive_column_is_rejected(datasource):
    with pytest.raises(SQLRejected):
        validate_sql(
            "SELECT email FROM public.customers LIMIT 5", datasource, {}
        )


def test_unknown_function_is_rejected(datasource):
    with pytest.raises(SQLRejected):
        validate_sql(
            "SELECT dangerous_udf(id) FROM public.orders LIMIT 5", datasource, {}
        )



def test_union_branch_limit_does_not_count_as_global_limit(datasource):
    result = validate_sql(
        "(SELECT id FROM public.orders LIMIT 1) UNION ALL "
        "SELECT id FROM public.customers",
        datasource,
        {},
    )
    assert result.has_top_level_limit is False
    assert result.limit_value is None
