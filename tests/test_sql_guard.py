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


def mysql_datasource():
    return DataSource(
        datasource_id="mysql",
        name="mysql",
        dialect="mysql",
        url_env="MYSQL_URL",
        readonly_mode="mysql_transaction",
        allowed_tables=frozenset({"sales.orders"}),
        sensitive_columns=frozenset({"payment_token"}),
        auto_execute=True,
        max_auto_scan_rows=1000,
        max_hard_scan_rows=10000,
        max_result_rows=100,
        max_result_bytes=100000,
        timeout_ms=1000,
        allowed_agents=frozenset({"agent"}),
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id /*!50000 , payment_token */ FROM sales.orders LIMIT 5",
        "SELECT id FROM sales.orders WHERE id=1--1 UNION SELECT password FROM mysql.user",
        "SELECT id FROM sales.orders /* optimizer hint */ LIMIT 5",
        "SELECT id FROM sales.orders # hidden comment",
    ],
)
def test_mysql_comment_and_parser_differential_payloads_are_rejected(sql):
    with pytest.raises(SQLRejected):
        validate_sql(sql, mysql_datasource(), {})


def test_mysql_backslash_and_double_quote_ambiguities_are_rejected():
    with pytest.raises(SQLRejected):
        validate_sql(
            "SELECT id FROM sales.orders WHERE note='a\\\\' OR 1=1",
            mysql_datasource(),
            {},
        )
    with pytest.raises(SQLRejected):
        validate_sql(
            'SELECT "id" FROM sales.orders LIMIT 5',
            mysql_datasource(),
            {},
        )


def test_bind_marker_inside_string_literal_is_rejected(datasource):
    with pytest.raises(SQLRejected, match="Bind markers inside quoted"):
        validate_sql(
            "SELECT ':payload' AS note, id FROM public.orders LIMIT 1",
            datasource,
            {"payload": "value"},
        )


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT id FROM "public"."orders" LIMIT 5',
        'SELECT id FROM "public.orders" LIMIT 5',
    ],
)
def test_quoted_or_ambiguous_table_identifiers_are_rejected(datasource, sql):
    with pytest.raises(SQLRejected):
        validate_sql(sql, datasource, {})


def test_fetch_first_is_recognised_as_top_level_limit(datasource):
    result = validate_sql(
        "SELECT id FROM public.orders FETCH FIRST 5 ROWS ONLY",
        datasource,
        {},
    )
    assert result.has_top_level_limit is True
    assert result.limit_value == 5
