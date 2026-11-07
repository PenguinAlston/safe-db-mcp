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


@pytest.mark.parametrize("value", [
    "email", "{email: blocked}", "null", "[true]", "[1]", "[null]", "[email, false]",
])
def test_sensitive_columns_requires_a_list_of_strings(tmp_path, value):
    config = tmp_path / "datasources.yaml"
    config.write_text(BASE + f"    sensitive_columns: {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sensitive_columns must be a list of strings"):
        load_datasources(str(config))


@pytest.mark.parametrize("value, expected", [
    ("[]", frozenset()),
    ("[EMAIL, password_hash]", frozenset({"email", "password_hash"})),
])
def test_sensitive_columns_accepts_string_lists(tmp_path, value, expected):
    config = tmp_path / "datasources.yaml"
    config.write_text(BASE + f"    sensitive_columns: {value}\n", encoding="utf-8")
    assert load_datasources(str(config))["pg"].sensitive_columns == expected


RESOURCE_FIELDS = [
    "max_auto_scan_rows", "max_hard_scan_rows", "max_result_rows",
    "max_result_bytes", "timeout_ms",
]


@pytest.mark.parametrize("key", RESOURCE_FIELDS)
@pytest.mark.parametrize("value", ["true", "false", "1.0", "0.9", '"10"', "null"])
def test_resource_values_require_actual_integers(tmp_path, key, value):
    config = tmp_path / "datasources.yaml"
    base = BASE.replace("    timeout_ms: 1000\n", "")
    config.write_text(base + f"    {key}: {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"{key} must be an integer"):
        load_datasources(str(config))


@pytest.mark.parametrize("key", RESOURCE_FIELDS)
def test_resource_values_accept_positive_integers(tmp_path, key):
    config = tmp_path / "datasources.yaml"
    base = BASE.replace("    timeout_ms: 1000\n", "")
    config.write_text(base + f"    {key}: 10\n", encoding="utf-8")
    assert getattr(load_datasources(str(config))["pg"], key) == 10


@pytest.mark.parametrize("key", ["max_auto_scan_rows", "max_hard_scan_rows"])
def test_scan_limits_accept_explicit_zero(tmp_path, key):
    config = tmp_path / "datasources.yaml"
    config.write_text(BASE + f"    {key}: 0\n", encoding="utf-8")
    assert getattr(load_datasources(str(config))["pg"], key) == 0


@pytest.mark.parametrize("key", RESOURCE_FIELDS)
def test_negative_resource_values_are_rejected(tmp_path, key):
    config = tmp_path / "datasources.yaml"
    base = BASE.replace("    timeout_ms: 1000\n", "")
    config.write_text(base + f"    {key}: -1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_datasources(str(config))


@pytest.mark.parametrize("key", ["max_result_rows", "max_result_bytes", "timeout_ms"])
def test_positive_resource_fields_reject_zero(tmp_path, key):
    config = tmp_path / "datasources.yaml"
    base = BASE.replace("    timeout_ms: 1000\n", "")
    config.write_text(base + f"    {key}: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"{key} must be greater than zero"):
        load_datasources(str(config))


def test_omitted_settings_keep_integer_defaults(tmp_path):
    config = tmp_path / "datasources.yaml"
    config.write_text(BASE.replace("    timeout_ms: 1000\n", ""), encoding="utf-8")
    datasource = load_datasources(str(config))["pg"]
    assert datasource.sensitive_columns == frozenset()
    expected = dict(zip(RESOURCE_FIELDS, [0, 0, 100, 524288, 5000]))
    for key, value in expected.items():
        assert getattr(datasource, key) == value
        assert type(getattr(datasource, key)) is int



def test_local_manual_credential_source_does_not_require_url_env(tmp_path):
    config = tmp_path / "datasources.yaml"
    config.write_text(
        """datasources:
  pg:
    dialect: postgres
    credential_source:
      type: local_manual
      max_ttl_minutes: 480
    readonly_mode: postgres_transaction
    allowed_agents: [agent]
    allowed_tables: [public.orders]
""",
        encoding="utf-8",
    )
    datasource = load_datasources(str(config))["pg"]
    assert datasource.url_env is None
    assert datasource.credential_mode == "local_manual"
    assert datasource.max_credential_ttl_minutes == 480


def test_manual_credential_source_rejects_url_env(tmp_path):
    config = tmp_path / "datasources.yaml"
    config.write_text(
        BASE + """    credential_source:
      type: local_manual
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        load_datasources(str(config))


@pytest.mark.parametrize("ttl", [0, -1, 481, "480", "true"])
def test_manual_credential_ttl_is_strict_and_bounded(tmp_path, ttl):
    config = tmp_path / "datasources.yaml"
    config.write_text(
        "datasources:\n  pg:\n    dialect: postgres\n"
        "    credential_source:\n      type: local_manual\n"
        "      max_ttl_minutes: " + str(ttl) + "\n"
        "    readonly_mode: postgres_transaction\n"
        "    allowed_agents: [agent]\n"
        "    allowed_tables: [public.orders]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_datasources(str(config))


def test_unknown_credential_source_type_fails_closed(tmp_path):
    config = tmp_path / "datasources.yaml"
    config.write_text(
        BASE.replace("    url_env: PG_URL\n", "")
        + """    credential_source:
      type: cloud_magic
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported credential_source"):
        load_datasources(str(config))
