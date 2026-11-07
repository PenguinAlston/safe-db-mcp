from __future__ import annotations

from safe_db_mcp.adapters.sqlalchemy_adapter import SQLAlchemyReadOnlyAdapter
from safe_db_mcp.config import DataSource


class AdapterUnavailable(RuntimeError):
    pass


def create_adapter(datasource: DataSource, credential_provider=None):
    expected_modes = {
        "postgres": "postgres_transaction",
        "mysql": "mysql_transaction",
        "oracle": "oracle_transaction",
    }
    expected_mode = expected_modes.get(datasource.dialect)
    if expected_mode is None:
        raise AdapterUnavailable(
            "No reviewed read-only adapter is enabled for dialect: "
            f"{datasource.dialect}"
        )
    if datasource.readonly_mode != expected_mode:
        raise AdapterUnavailable(
            "Datasource read-only mode does not match its adapter"
        )
    return SQLAlchemyReadOnlyAdapter(
        datasource.dialect,
        datasource.database_url(credential_provider),
        datasource.timeout_ms,
    )
