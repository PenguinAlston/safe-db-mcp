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
    url_env: str
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

    def database_url(self) -> str:
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
    try:
        value = int(item.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return value


def _nonnegative_int(item: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(item.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
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

        result[datasource_id] = DataSource(
            datasource_id=datasource_id,
            name=str(item.get("name", datasource_id)),
            dialect=dialect,
            url_env=str(item["url_env"]),
            readonly_mode=str(item.get("readonly_mode", "unsupported")),
            allowed_tables=allowed,
            sensitive_columns=frozenset(
                normalise_identifier(str(x)) for x in item.get("sensitive_columns", [])
            ),
            auto_execute=auto_execute,
            max_auto_scan_rows=_nonnegative_int(item, "max_auto_scan_rows", 0),
            max_hard_scan_rows=_nonnegative_int(item, "max_hard_scan_rows", 0),
            max_result_rows=_positive_int(item, "max_result_rows", 100),
            max_result_bytes=_positive_int(item, "max_result_bytes", 524288),
            timeout_ms=_positive_int(item, "timeout_ms", 5000),
            allowed_agents=agents,
        )

    if not result:
        raise ValueError("At least one datasource must be configured")
    return result
