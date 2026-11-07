from __future__ import annotations

import pytest

from safe_db_mcp.config import load_datasources, normalise_identifier


BASE = """datasources:
  pg:
    dialect: postgres
    url_env: PG_URL
    readonly_mode: postgres_transaction
    allowed_agents: [agent]
    allowed_tables: [public.orders]
    timeout_ms: 1000
"""


def test_missing_datasource_config_fails_closed(tmp_path):
    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError):
        load_datasources(str(missing))


def test_identifier_normalisation_rejects_quoted_dot_collision():
    with pytest.raises(ValueError):
        normalise_identifier('"public.orders"')
    with pytest.raises(ValueError):
        normalise_identifier('"public"."orders"')
    assert normalise_identifier("PUBLIC.ORDERS") == "public.orders"


def test_timeout_must_be_positive(tmp_path):
    config = tmp_path / "datasources.yaml"
    config.write_text(BASE.replace("timeout_ms: 1000", "timeout_ms: 0"), encoding="utf-8")
    with pytest.raises(ValueError, match="timeout_ms must be greater than zero"):
        load_datasources(str(config))


def test_result_limits_must_be_positive(tmp_path):
    config = tmp_path / "datasources.yaml"
    config.write_text(
        BASE.replace("timeout_ms: 1000", "timeout_ms: 1000\n    max_result_bytes: 0"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_result_bytes must be greater than zero"):
        load_datasources(str(config))
