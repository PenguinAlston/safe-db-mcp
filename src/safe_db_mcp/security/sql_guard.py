from __future__ import annotations

import hashlib
import json
import re
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


def _reject_ambiguous_lexemes(sql: str, dialect: str) -> None:
    # SQL comments are disabled entirely. This blocks MySQL executable comments,
    # optimizer hints, --1 parser differentials, and ordinary comment carriers.
    if "--" in sql or "/*" in sql or "*/" in sql:
        raise SQLRejected("SQL comments are disabled")
    # Backslash quoting/escaping is mode-dependent across supported engines.
    # Reject it globally so the SQLAlchemy text() scanner and server lexer agree.
    if "\\" in sql:
        raise SQLRejected("Backslash escapes are disabled")
    if dialect == "mysql":
        # MySQL lexical interpretation changes with sql_mode. Require a narrow,
        # mode-independent subset instead of guessing the server's active mode.
        if "#" in sql or "\\" in sql or '"' in sql:
            raise SQLRejected(
                "MySQL # comments, backslash escapes and double quotes are disabled"
            )


def _reject_bind_markers_in_quoted_text(sql: str) -> None:
    """Reject :name tokens in quoted spans before SQLAlchemy's text() parser sees them."""
    i = 0
    quote: str | None = None
    while i < len(sql):
        char = sql[i]
        if quote is not None:
            if char == ":" and i + 1 < len(sql) and re.match(r"\w", sql[i + 1]):
                raise SQLRejected("Bind markers inside quoted SQL text are disabled")
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            i += 1
            continue
        if char == "$":
            match = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if match:
                delimiter = match.group(0)
                end = sql.find(delimiter, i + len(delimiter))
                if end < 0:
                    raise SQLRejected("Unterminated dollar-quoted SQL text")
                inner = sql[i + len(delimiter):end]
                if re.search(r"(?<!:):[A-Za-z_0-9]+", inner):
                    raise SQLRejected(
                        "Bind markers inside quoted SQL text are disabled"
                    )
                i = end + len(delimiter)
                continue
        i += 1


def _table_key(table: exp.Table, dialect: str) -> str:
    schema_node = table.args.get("db")
    name_node = table.args.get("this")
    if (
        not isinstance(schema_node, exp.Identifier)
        or not isinstance(name_node, exp.Identifier)
        or schema_node.args.get("quoted")
        or name_node.args.get("quoted")
    ):
        raise SQLRejected("Only simple, unquoted schema-qualified table names are allowed")

    schema = str(schema_node.this)
    name = str(name_node.this)
    if dialect == "mysql" and (schema != schema.casefold() or name != name.casefold()):
        raise SQLRejected("MySQL table identifiers must use lowercase spelling")
    return normalise_identifier(f"{schema}.{name}")


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

    _reject_ambiguous_lexemes(sql, datasource.dialect)
    _reject_bind_markers_in_quoted_text(sql)

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
        if getattr(node, "comments", None):
            raise SQLRejected("SQL comments are disabled")
        if type(node).__name__ in FORBIDDEN_NODE_NAMES:
            forbidden.append(type(node).__name__)
        for arg_name in FORBIDDEN_ARGS:
            if node.args.get(arg_name) is not None:
                forbidden.append(f"{type(node).__name__}.{arg_name}")

        if isinstance(node, exp.Table):
            if node.catalog:
                raise SQLRejected("Cross-catalog references are not allowed")
            key = _table_key(node, datasource.dialect)
            if key not in datasource.allowed_tables:
                raise SQLRejected(f"Table is not allowlisted: {key}")
            tables.add(key)

        if isinstance(node, exp.Column):
            if isinstance(node.this, exp.Identifier) and node.this.args.get("quoted"):
                raise SQLRejected("Quoted column identifiers are disabled")
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

    # A branch-local LIMIT does not bound a UNION/INTERSECT/EXCEPT result.
    # FETCH FIRST is accepted only when attached to the root expression.
    limit_node = tree.args.get("limit")
    fetch_node = tree.args.get("fetch")
    top_level_cap = limit_node if limit_node is not None else fetch_node
    has_limit = top_level_cap is not None
    limit_value = None
    if top_level_cap is not None:
        count_node = (
            top_level_cap.args.get("expression")
            or top_level_cap.args.get("count")
            or top_level_cap.this
        )
        if isinstance(count_node, exp.Literal) and not count_node.is_string:
            try:
                limit_value = int(count_node.this)
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
