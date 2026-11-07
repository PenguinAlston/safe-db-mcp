from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

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
SAFE_CAST_TYPES = {
    exp.DataType.Type.BIGINT, exp.DataType.Type.BOOLEAN, exp.DataType.Type.CHAR,
    exp.DataType.Type.DATE, exp.DataType.Type.DECIMAL, exp.DataType.Type.DOUBLE,
    exp.DataType.Type.FLOAT, exp.DataType.Type.INT, exp.DataType.Type.SMALLINT,
    exp.DataType.Type.TEXT, exp.DataType.Type.TIME, exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.VARCHAR,
}
# SQLGlot's special parsers omit source positions for these exact node types.
# Their original token spellings are checked separately before parsing.
FUNCTIONS_WITHOUT_META = {
    exp.Cast, exp.Ceil, exp.CurrentDate, exp.CurrentTimestamp,
    exp.Extract, exp.Floor, exp.Substring, exp.Trim,
}
EXTRACT_FIELDS = {"YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND"}
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
            match = re.match(r"\$\w*\$", sql[i:])
            if match:
                delimiter = match.group(0)
                end = sql.find(delimiter, i + len(delimiter))
                if end < 0:
                    raise SQLRejected("Unterminated dollar-quoted SQL text")
                inner = sql[i + len(delimiter):end]
                if re.search(r"(?<!:):\w+", inner):
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


def _reject_special_function_spellings(sql: str, dialect: str) -> None:
    dialect_impl = sqlglot.Dialect.get_or_raise(dialect)
    parser = dialect_impl.parser_class
    tokens = dialect_impl.tokenize(sql)
    for index, token in enumerate(tokens):
        special_call = (
            token.text.upper() in parser.FUNCTION_PARSERS
            and index + 1 < len(tokens)
            and tokens[index + 1].token_type == TokenType.L_PAREN
        )
        if special_call or token.token_type in parser.NO_PAREN_FUNCTIONS:
            original = sql[token.start:token.end + 1]
            if not re.fullmatch(r"[A-Za-z_]+", original) or original.upper() not in SAFE_FUNCTIONS:
                raise SQLRejected("Function spelling is not allowlisted")


def _reject_quoted_cast_targets(sql: str, dialect: str) -> None:
    # SQLGlot canonicalises a quoted cast target (e.g. CAST(x AS "TEXT")) into the
    # same built-in type enum as its unquoted form, so the AST cannot reveal that
    # the original spelling was quoted. Scan the token stream instead: a quoted
    # IDENTIFIER after "AS" inside a CAST body or after "::" is never allowlisted.
    dialect_impl = sqlglot.Dialect.get_or_raise(dialect)
    tokens = dialect_impl.tokenize(sql)
    cast_body_depths: set[int] = set()
    depth = 0
    prev_is_cast = False
    for index, token in enumerate(tokens):
        ttype = token.token_type
        if ttype == TokenType.L_PAREN:
            depth += 1
            if prev_is_cast:
                cast_body_depths.add(depth)
        elif ttype == TokenType.R_PAREN:
            cast_body_depths.discard(depth)
            depth -= 1
        else:
            quoted_target = (
                index + 1 < len(tokens)
                and (
                    (ttype == TokenType.ALIAS and depth in cast_body_depths)
                    or ttype == TokenType.DCOLON
                )
                and tokens[index + 1].token_type == TokenType.IDENTIFIER
            )
            if quoted_target:
                raise SQLRejected("Cast target must be an allowlisted built-in scalar type")
        prev_is_cast = token.text.upper() == "CAST"


def _function_name(node: exp.Func, sql: str) -> str:
    start, end = node.meta.get("start"), node.meta.get("end")
    if start is not None or end is not None:
        if type(start) is int and type(end) is int and 0 <= start <= end < len(sql):
            original = sql[start:end + 1]
            if re.fullmatch(r"[A-Za-z_]+", original):
                return original.upper()
        return ""
    # Never trust a canonical name for an arbitrary node lacking source metadata.
    return node.sql_name() if type(node) in FUNCTIONS_WITHOUT_META else ""


def validate_sql(sql: str, datasource: DataSource, parameters: dict) -> ValidatedQuery:
    if not sql or not sql.strip():
        raise SQLRejected("SQL is empty")
    if len(sql) > 50000:
        raise SQLRejected("SQL exceeds the configured length limit")

    _reject_ambiguous_lexemes(sql, datasource.dialect)
    _reject_bind_markers_in_quoted_text(sql)

    try:
        _reject_special_function_spellings(sql, datasource.dialect)
        _reject_quoted_cast_targets(sql, datasource.dialect)
        parsed = sqlglot.parse(sql, read=datasource.dialect)
    except (ParseError, TokenError) as exc:
        raise SQLRejected("SQL could not be parsed for the selected dialect") from exc
    except SQLRejected:
        raise
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

    # Collect before walking projections: a bare relation name can expose its
    # entire row, including sensitive fields, even inside CAST or other functions.
    relation_names: set[str] = set()
    for relation in tree.find_all(exp.Table, exp.Subquery):
        if isinstance(relation, exp.Table):
            relation_names.add(relation.name.casefold())
        if relation.alias:
            relation_names.add(relation.alias.casefold())

    forbidden = []
    tables: set[str] = set()
    columns: set[str] = set()

    for node in tree.walk():
        if getattr(node, "comments", None):
            raise SQLRejected("SQL comments are disabled")
        extract_field = (
            isinstance(node, exp.Var)
            and type(node.parent) is exp.Extract
            and node.arg_key == "this"
            and node.name.upper() in EXTRACT_FIELDS
        )
        if type(node).__name__ in FORBIDDEN_NODE_NAMES and not extract_field:
            forbidden.append(type(node).__name__)
        for arg_name in FORBIDDEN_ARGS:
            if node.args.get(arg_name) is not None:
                forbidden.append(f"{type(node).__name__}.{arg_name}")

        if isinstance(node, exp.Dot) and node.find(exp.Func) is not None:
            raise SQLRejected("Qualified function invocations are disabled")
        if isinstance(node, exp.Operator):
            raise SQLRejected("Explicit operator invocations are disabled")
        if isinstance(node, exp.Cast):
            target = node.args.get("to")
            if (
                not isinstance(target, exp.DataType)
                or target.this not in SAFE_CAST_TYPES
                or target.args.get("nested")
                or target.args.get("kind")
                or any(isinstance(child, exp.DataType) for child in target.iter_expressions())
            ):
                raise SQLRejected("Cast target must be an allowlisted built-in scalar type")
        if isinstance(node, exp.TableAlias) and node.args.get("columns"):
            raise SQLRejected("Relation column alias lists are disabled")
        if isinstance(node, exp.LimitOptions) and (
            node.args.get("with_ties") or node.args.get("percent")
        ):
            raise SQLRejected("FETCH WITH TIES and PERCENT are disabled")

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
            if column_name in relation_names:
                raise SQLRejected("Whole-row references are disabled")
            if column_name:
                columns.add(column_name)
                if column_name in datasource.sensitive_columns:
                    raise SQLRejected(f"Sensitive column is blocked: {column_name}")

        # Conservative MVP: wildcard projections and COUNT(*) are both blocked.
        if isinstance(node, exp.Star):
            raise SQLRejected("Wildcard expressions are not allowed")

        if isinstance(node, exp.Func):
            function_name = _function_name(node, sql)
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
