from __future__ import annotations

from safe_db_mcp.adapters.sqlalchemy_adapter import SQLAlchemyReadOnlyAdapter
from safe_db_mcp.config import DataSource


class AdapterUnavailable(RuntimeError):
    pass


def create_adapter(datasource: DataSource):
    # Fail closed for dialects without a reviewed native read-only implementation.
    if datasource.dialect not in {"postgres", "mysql"}:
        raise AdapterUnavailable(
            f"No reviewed read-only adapter is enabled for dialect: {datasource.dialect}"
        )
    expected_mode = {
        "postgres": "postgres_transaction",
        "mysql": "mysql_transaction",
    }[datasource.dialect]
    if datasource.readonly_mode != expected_mode:
        raise AdapterUnavailable("Datasource read-only mode does not match its adapter")
    return SQLAlchemyReadOnlyAdapter(
        datasource.dialect, datasource.database_url()
    )
