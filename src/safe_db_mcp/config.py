from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class DataSource:
    datasource_id: str
    name: str
    dialect: str
    url_env: str | None
    readonly_mode: str
    allowed_tables: frozenset[str]
    sensitive_columns: frozenset[str]
    auto_execute: bool
    max_auto_scan_rows: int
    max_hard_scan_rows: int
    max_result_rows: int
    max_result_bytes: int
    timeout_ms: int
    allowed_agents: frozenset[str] = frozenset()
    credential_mode: str = "environment"
    max_credential_ttl_minutes: int = 480

    def database_url(self, credential_provider=None) -> str:
        if self.credential_mode == "local_manual":
            if credential_provider is None:
                raise RuntimeError("Datasource requires a Worker-local credential")
            return credential_provider.get_url(self.datasource_id)
        if not self.url_env:
            raise RuntimeError("Datasource environment credential is not configured")
        value = os.getenv(self.url_env, "").strip()
        if not value:
            raise RuntimeError(
                f"Datasource secret is not configured: {self.url_env}"
            )
        return value


def normalise_identifier(value: str) -> str:
    """Canonicalise only simple, unquoted identifiers.

    Quoted/qualified-name parsing is intentionally not guessed here: accepting
    lossy quote stripping can make the SQL guard and database resolve different
    objects. Callers that need a table key must supply exactly schema.table.
    """
    if not isinstance(value, str):
        raise ValueError("Identifier must be a string")
    parts = value.split(".")
    if not 1 <= len(parts) <= 2 or any(not _IDENTIFIER.fullmatch(p) for p in parts):
        raise ValueError(f"Unsupported or ambiguous identifier: {value!r}")
    return ".".join(part.casefold() for part in parts)


def _positive_int(item: dict[str, Any], key: str, default: int) -> int:
    value = item.get(key, default)
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    if value <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return value


def _nonnegative_int(item: dict[str, Any], key: str, default: int) -> int:
    value = item.get(key, default)
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    if value < 0:
        raise ValueError(f"{key} must not be negative")
    return value


def load_datasources(path: str | None = None) -> dict[str, DataSource]:
    config_path = Path(
        path or os.getenv("SAFEDB_DATASOURCES_FILE", "./config/datasources.yaml")
    )
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Datasource configuration file not found: {config_path}"
        )

    raw: dict[str, Any] = yaml.safe_load(
        config_path.read_text(encoding="utf-8")
    ) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("datasources"), dict):
        raise ValueError("Datasource configuration must contain a datasources mapping")

    result: dict[str, DataSource] = {}
    for datasource_id, item in raw["datasources"].items():
        if not isinstance(datasource_id, str) or not datasource_id.replace("_", "").isalnum():
            raise ValueError(f"Invalid datasource ID: {datasource_id}")
        if not isinstance(item, dict):
            raise ValueError(f"{datasource_id}: datasource entry must be a mapping")

        allowed_values = item.get("allowed_tables", [])
        if not isinstance(allowed_values, list) or not allowed_values:
            raise ValueError(f"{datasource_id}: allowed_tables must not be empty")
        allowed_items = []
        for value in allowed_values:
            key = normalise_identifier(value)
            if "." not in key:
                raise ValueError(
                    f"{datasource_id}: allowed table must be schema-qualified: {value}"
                )
            allowed_items.append(key)
        allowed = frozenset(allowed_items)

        agent_values = item.get("allowed_agents", [])
        if not isinstance(agent_values, list):
            raise ValueError(f"{datasource_id}: allowed_agents must be a list")
        agents = frozenset(str(x) for x in agent_values if str(x))
        if not agents:
            raise ValueError(f"{datasource_id}: allowed_agents must not be empty")

        auto_execute = item.get("auto_execute", False)
        if not isinstance(auto_execute, bool):
            raise ValueError(f"{datasource_id}: auto_execute must be a YAML boolean")

        dialect = str(item.get("dialect", "")).casefold()
        if dialect not in {"mysql", "postgres", "oracle"}:
            raise ValueError(f"{datasource_id}: unsupported dialect: {dialect}")

        sensitive_values = item.get("sensitive_columns", [])
        if not isinstance(sensitive_values, list) or any(
            not isinstance(value, str) for value in sensitive_values
        ):
            raise ValueError(f"{datasource_id}: sensitive_columns must be a list of strings")
        sensitive = frozenset(normalise_identifier(value) for value in sensitive_values)
        if any("." in key for key in sensitive):
            raise ValueError(
                f"{datasource_id}: sensitive_columns must use unqualified column names"
            )

        credential_source = item.get("credential_source")
        if credential_source is None:
            if not isinstance(item.get("url_env"), str) or not item["url_env"].strip():
                raise ValueError(
                    f"{datasource_id}: url_env is required unless credential_source is configured"
                )
            credential_mode = "environment"
            max_credential_ttl_minutes = 480
            url_env = item["url_env"].strip()
        else:
            if not isinstance(credential_source, dict):
                raise ValueError(f"{datasource_id}: credential_source must be a mapping")
            credential_mode = credential_source.get("type")
            if credential_mode == "local_manual":
                if "url_env" in item:
                    raise ValueError(
                        f"{datasource_id}: url_env cannot be combined with local_manual credentials"
                    )
                max_credential_ttl_minutes = _positive_int(
                    credential_source, "max_ttl_minutes", 480
                )
                if max_credential_ttl_minutes > 480:
                    raise ValueError(
                        f"{datasource_id}: max_ttl_minutes cannot exceed 480"
                    )
                url_env = None
            elif credential_mode == "environment":
                if not isinstance(item.get("url_env"), str) or not item["url_env"].strip():
                    raise ValueError(
                        f"{datasource_id}: url_env is required for environment credentials"
                    )
                max_credential_ttl_minutes = 480
                url_env = item["url_env"].strip()
            else:
                raise ValueError(
                    f"{datasource_id}: unsupported credential_source type"
                )

        result[datasource_id] = DataSource(
            datasource_id=datasource_id,
            name=str(item.get("name", datasource_id)),
            dialect=dialect,
            url_env=url_env,
            readonly_mode=str(item.get("readonly_mode", "unsupported")),
            allowed_tables=allowed,
            sensitive_columns=sensitive,
            auto_execute=auto_execute,
            max_auto_scan_rows=_nonnegative_int(item, "max_auto_scan_rows", 0),
            max_hard_scan_rows=_nonnegative_int(item, "max_hard_scan_rows", 0),
            max_result_rows=_positive_int(item, "max_result_rows", 100),
            max_result_bytes=_positive_int(item, "max_result_bytes", 524288),
            timeout_ms=_positive_int(item, "timeout_ms", 5000),
            allowed_agents=agents,
            credential_mode=credential_mode,
            max_credential_ttl_minutes=max_credential_ttl_minutes,
        )

    if not result:
        raise ValueError("At least one datasource must be configured")
    return result
