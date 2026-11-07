from dataclasses import replace

import pytest
from sqlalchemy import text

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
        sensitive_columns={"password_hash": "mask", "email": "redact"},
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
    "CREATE TEMPORARY TABLE t AS SELECT id FROM public.orders",
    "CREATE TEMP TABLE t AS SELECT id FROM public.orders",
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


def test_bare_sensitive_projection_is_allowed_for_redaction(datasource):
    result = validate_sql(
        "SELECT email FROM public.customers LIMIT 5", datasource, {}
    )
    assert "email" in result.columns
    result = validate_sql(
        "SELECT email AS email FROM public.customers LIMIT 5", datasource, {}
    )
    assert "email" in result.columns
    result = validate_sql(
        "SELECT c.email FROM public.customers AS c LIMIT 5", datasource, {}
    )
    assert "email" in result.columns
    result = validate_sql(
        "SELECT email FROM (SELECT email FROM public.customers) sub LIMIT 5",
        datasource, {},
    )
    assert "email" in result.columns


@pytest.mark.parametrize("sql", [
    "SELECT email AS e FROM public.customers LIMIT 5",
    "SELECT LOWER(email) FROM public.customers LIMIT 5",
    "SELECT LOWER(c.email) FROM public.customers AS c LIMIT 5",
    "SELECT id FROM public.orders WHERE email = 'x' LIMIT 5",
    "SELECT id FROM public.orders ORDER BY email LIMIT 5",
    "SELECT id FROM public.customers UNION SELECT email FROM public.customers",
    "SELECT email FROM (SELECT email AS e FROM public.customers) sub LIMIT 5",
    "SELECT UPPER(email) FROM public.customers GROUP BY UPPER(email) LIMIT 5",
])
def test_sensitive_values_may_not_escape_redacted_output_name(datasource, sql):
    with pytest.raises(SQLRejected, match="Sensitive column|Set operations"):
        validate_sql(sql, datasource, {})


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
        sensitive_columns={"payment_token": "hash"},
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


@pytest.mark.parametrize("expression", [
    "private.lower(id)",
    "public.lower(id)",
    '"private".lower(id)',
    '"LOWER"(id)',
    '"lower"(id)',
    "LCASE(id)",
    "UCASE(id)",
    '"CEIL"(id)',
    '"CAST"(id AS TEXT)',
    '"EXTRACT"(YEAR FROM id)',
    '"TRIM"(id)',
    '"SUBSTRING"(id, 1)',
    "CONVERT(id, TEXT)",
    "DATE_PART('year', id)",
])
def test_function_identity_cannot_be_canonicalised_into_allowlist(datasource, expression):
    with pytest.raises(SQLRejected, match="[Ff]unction"):
        validate_sql(f"SELECT {expression} FROM public.orders LIMIT 5", datasource, {})


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle"])
@pytest.mark.parametrize("expression", [
    "lower(id)",
    "CEIL(id)",
    "CEILING(id)",
    "FLOOR(id)",
    "SUBSTR(id, 1, 2)",
    "SUBSTRING(id, 1, 2)",
    "TRIM(id)",
    "CAST(id AS INTEGER)",
    "EXTRACT(YEAR FROM created_at)",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
])
def test_existing_unquoted_safe_functions_remain_valid(datasource, dialect, expression):
    result = validate_sql(
        f"SELECT {expression} FROM public.orders",
        replace(datasource, dialect=dialect),
        {},
    )
    assert result.tables == ("public.orders",)


@pytest.mark.parametrize("expression", [
    "id OPERATOR(private.+) 1",
    "id OPERATOR(pg_catalog.+) 1",
])
def test_explicit_operator_invocations_are_rejected(datasource, expression):
    with pytest.raises(SQLRejected, match="[Oo]perator"):
        validate_sql(f"SELECT {expression} FROM public.orders", datasource, {})


@pytest.mark.parametrize("target", [
    "private.custom_type", "custom_domain", "public.orders", "RECORD",
    "INTEGER[]", "TEXT[][]", "STRUCT<id INTEGER>", "MAP<INT, TEXT>",
])
def test_non_scalar_or_custom_cast_targets_are_rejected(datasource, target):
    with pytest.raises(SQLRejected, match="[Cc]ast"):
        validate_sql(f"SELECT CAST(id AS {target}) FROM public.orders", datasource, {})


@pytest.mark.parametrize("expression", [
    'CAST(id AS "TEXT")', 'id::"TEXT"', 'CAST(id AS "integer")',
    'CAST(COALESCE(id, 1) AS "TEXT")',
])
def test_quoted_cast_targets_cannot_be_canonicalised(datasource, expression):
    with pytest.raises(SQLRejected, match="[Cc]ast"):
        validate_sql(f"SELECT {expression} FROM public.orders", datasource, {})


@pytest.mark.parametrize("target", ["INTEGER", "TEXT", "DECIMAL", "DECIMAL(10, 2)"])
def test_builtin_scalar_cast_targets_remain_valid(datasource, target):
    result = validate_sql(f"SELECT CAST(id AS {target}) FROM public.orders", datasource, {})
    assert result.columns == ("id",)


@pytest.mark.parametrize("sql", [
    "SELECT c.renamed FROM public.customers AS c(id, renamed)",
    "SELECT c.renamed FROM (SELECT id FROM public.orders) AS c(renamed)",
])
def test_relation_column_alias_lists_are_rejected(datasource, sql):
    with pytest.raises(SQLRejected, match="[Aa]lias"):
        validate_sql(sql, datasource, {})


@pytest.mark.parametrize("sql", [
    "SELECT c FROM public.orders AS c",
    "SELECT CAST(c AS TEXT) FROM public.orders AS c",
    "SELECT orders FROM public.orders",
    "SELECT public.orders FROM public.orders",
    "SELECT c FROM (SELECT id FROM public.orders) AS c",
    "SELECT CAST(c AS TEXT) FROM (SELECT id FROM public.orders) AS c",
    "SELECT C FROM public.orders AS c",
    "SELECT c FROM public.orders AS \"c\"",
])
def test_whole_row_references_are_rejected(datasource, sql):
    with pytest.raises(SQLRejected, match="[Ww]hole.row"):
        validate_sql(sql, datasource, {})


@pytest.mark.parametrize("sql", [
    "SELECT c.id FROM public.orders c",
    "SELECT public.orders.id FROM public.orders",
    "SELECT c.id FROM (SELECT id FROM public.orders) c",
])
def test_qualified_column_references_remain_valid(datasource, sql):
    assert validate_sql(sql, datasource, {}).columns == ("id",)


@pytest.mark.parametrize("dialect", ["postgres", "oracle"])
@pytest.mark.parametrize("clause", [
    "FETCH FIRST 5 ROWS WITH TIES",
    "FETCH FIRST 5 PERCENT ROWS ONLY",
    "FETCH FIRST 5 PERCENT ROWS WITH TIES",
])
def test_fetch_ties_and_percent_are_not_literal_row_caps(datasource, dialect, clause):
    with pytest.raises(SQLRejected, match="WITH TIES|PERCENT"):
        validate_sql(
            f"SELECT id FROM public.orders ORDER BY id {clause}",
            replace(datasource, dialect=dialect),
            {},
        )


@pytest.mark.parametrize("literal", [
    "$$:名$$", "$tag$:名$tag$", "$名$:名$名$", "$tag$:payload$tag$", "':名'",
])
def test_unicode_bind_markers_in_quoted_text_are_rejected(datasource, literal):
    sql = f"SELECT {literal} AS note FROM public.orders LIMIT 1"
    assert text(sql).compile().params  # SQLAlchemy scans inside all these quoted spans.
    with pytest.raises(SQLRejected, match="Bind markers inside quoted"):
        validate_sql(sql, datasource, {})


@pytest.mark.parametrize("name", ["id", "名"])
def test_normal_bind_markers_remain_valid(datasource, name):
    sql = f"SELECT id FROM public.orders WHERE id = :{name} LIMIT 1"
    assert set(text(sql).compile().params) == {name}
    assert validate_sql(sql, datasource, {name: 1}).columns == ("id",)
