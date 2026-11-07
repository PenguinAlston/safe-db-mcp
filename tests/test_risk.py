from safe_db_mcp.config import DataSource
from safe_db_mcp.security.risk import QueryPlan, assess_risk
from safe_db_mcp.security.sql_guard import validate_sql


def test_low_risk_auto_requires_verified_readonly_and_plan():
    ds = DataSource(
        "db", "test", "postgres", "DB_URL", "postgres_transaction",
        frozenset({"public.orders"}), frozenset(), True,
        1000, 10000, 100, 100000, 1000,
    )
    query = validate_sql("SELECT id FROM public.orders LIMIT 10", ds, {})
    plan = QueryPlan(100, 10, {})
    assessed = assess_risk(query, plan, ds, True)
    assert assessed.level == "LOW"
    assert assessed.auto_eligible


def test_missing_estimate_never_auto_executes():
    ds = DataSource(
        "db", "test", "postgres", "DB_URL", "postgres_transaction",
        frozenset({"public.orders"}), frozenset(), True,
        1000, 10000, 100, 100000, 1000,
    )
    query = validate_sql("SELECT id FROM public.orders LIMIT 10", ds, {})
    assessed = assess_risk(query, QueryPlan(None, None, {}), ds, True)
    assert assessed.level != "LOW"
    assert not assessed.auto_eligible


def test_unverified_readonly_never_auto_executes():
    ds = DataSource(
        "db", "test", "postgres", "DB_URL", "postgres_transaction",
        frozenset({"public.orders"}), frozenset(), True,
        1000, 10000, 100, 100000, 1000,
    )
    query = validate_sql("SELECT id FROM public.orders LIMIT 10", ds, {})
    assessed = assess_risk(query, QueryPlan(100, 10, {}), ds, False)
    assert not assessed.auto_eligible
