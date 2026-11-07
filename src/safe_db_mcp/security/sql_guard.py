from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from safe_db_mcp.config import DataSource, normalise_identifier


class SQLRejected(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedQuery:
    original_sql: str
    sql_hash: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    has_top_level_limit: bool
    limit_value: int | None
    join_count: int
    select_count: int


SAFE_FUNCTIONS = {
    "ABS", "AVG", "CAST", "CEIL", "CEILING", "COALESCE", "CONCAT",
    "COUNT", "CURRENT_DATE", "CURRENT_TIMESTAMP", "DATE", "DATE_TRUNC",
    "EXTRACT", "FLOOR", "LENGTH", "LOWER", "MAX", "MIN", "NULLIF",
    "ROUND", "SUBSTR", "SUBSTRING", "SUM", "TRIM", "UPPER",
}
FORBIDDEN_NODE_NAMES = {
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
    "Command", "Copy", "Grant", "Revoke", "Transaction", "Set", "Use",
    "Pragma", "Attach", "Detach", "Lock", "Into", "SessionParameter",
    "SystemParameter", "SystemVariable", "Var", "Declare", "Execute",
}
FORBIDDEN_ARGS = {"into", "lock", "outfile", "dumpfile", "file", "procedure"}


def _table_key(table: exp.Table) -> str:
    parts = [p for p in (table.db, table.name) if p]
    return normalise_identifier(".".join(parts))


def _function_name(node: exp.Func) -> str:
    name = node.sql_name()
    if not name or name.upper() == "ANONYMOUS":
        name = getattr(node, "name", "") or ""
    return str(name).upper()


def validate_sql(sql: str, datasource: DataSource, parameters: dict) -> ValidatedQuery:
    if not sql or not sql.strip():
        raise SQLRejected("SQL is empty")
    if len(sql) > 50000:
        raise SQLRejected("SQL exceeds the configured length limit")

    try:
        parsed = sqlglot.parse(sql, read=datasource.dialect)
    except ParseError as exc:
        raise SQLRejected("SQL could not be parsed for the selected dialect") from exc
    except (ValueError, TypeError) as exc:
        raise SQLRejected("SQL parser failed closed") from exc

    statements = [tree for tree in parsed if tree is not None]
    if len(statements) != 1:
        raise SQLRejected("Exactly one SQL statement is required")

    tree = statements[0]
    allowed_roots = tuple(
        cls for cls in (
            getattr(exp, "Select", None),
            getattr(exp, "Union", None),
            getattr(exp, "Intersect", None),
            getattr(exp, "Except", None),
        ) if cls is not None
    )
    if not isinstance(tree, allowed_roots):
        raise SQLRejected("Only SELECT queries are allowed")

    # CTE name resolution is dialect- and scope-sensitive. Until the resolver
    # maps every CTE reference and underlying base table safely, reject all CTEs.
    if any(isinstance(node, exp.CTE) for node in tree.walk()):
        raise SQLRejected("CTEs are disabled until scoped object resolution is implemented")

    forbidden = []
    tables: set[str] = set()
    columns: set[str] = set()

    for node in tree.walk():
        if type(node).__name__ in FORBIDDEN_NODE_NAMES:
            forbidden.append(type(node).__name__)
        for arg_name in FORBIDDEN_ARGS:
            if node.args.get(arg_name) is not None:
                forbidden.append(f"{type(node).__name__}.{arg_name}")

        if isinstance(node, exp.Table):
            if node.catalog:
                raise SQLRejected("Cross-catalog references are not allowed")
            key = _table_key(node)
            if key not in datasource.allowed_tables:
                raise SQLRejected(f"Table is not allowlisted: {key}")
            tables.add(key)

        if isinstance(node, exp.Column):
            column_name = normalise_identifier(node.name)
            if column_name:
                columns.add(column_name)
                if column_name in datasource.sensitive_columns:
                    raise SQLRejected(f"Sensitive column is blocked: {column_name}")

        # Conservative MVP: wildcard projections and COUNT(*) are both blocked.
        if isinstance(node, exp.Star):
            raise SQLRejected("Wildcard expressions are not allowed")

        if isinstance(node, exp.Func):
            function_name = _function_name(node)
            if function_name not in SAFE_FUNCTIONS:
                raise SQLRejected(
                    f"Function is not allowlisted: {function_name or type(node).__name__}"
                )

    if forbidden:
        raise SQLRejected(f"Forbidden SQL construct: {forbidden[0]}")

    top_select = tree if isinstance(tree, exp.Select) else next(tree.find_all(exp.Select), None)
    limit_node = top_select.args.get("limit") if top_select is not None else None
    has_limit = limit_node is not None
    limit_value = None
    if limit_node is not None:
        limit_expression = limit_node.args.get("expression")
        if isinstance(limit_expression, exp.Literal) and not limit_expression.is_string:
            try:
                limit_value = int(limit_expression.this)
            except (TypeError, ValueError):
                limit_value = None

    joins = sum(1 for _ in tree.find_all(exp.Join))
    selects = sum(1 for _ in tree.find_all(exp.Select))

    digest_payload = json.dumps(
        {"sql": sql, "parameters": parameters},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    sql_hash = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()

    return ValidatedQuery(
        original_sql=sql,
        sql_hash=sql_hash,
        tables=tuple(sorted(tables)),
        columns=tuple(sorted(columns)),
        has_top_level_limit=has_limit,
        limit_value=limit_value,
        join_count=joins,
        select_count=max(1, selects),
    )
