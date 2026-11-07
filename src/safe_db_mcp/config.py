from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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
    return ".".join(
        part.strip().strip(chr(96) + '"[]').casefold() for part in value.split(".")
    )


def load_datasources(path: str | None = None) -> dict[str, DataSource]:
    config_path = Path(path or os.getenv(
        "SAFEDB_DATASOURCES_FILE", "./config/datasources.yaml"
    ))
    if not config_path.exists():
        config_path = Path("./config/datasources.example.yaml")
    raw: dict[str, Any] = yaml.safe_load(
        config_path.read_text(encoding="utf-8")
    ) or {}
    result: dict[str, DataSource] = {}

    for datasource_id, item in (raw.get("datasources") or {}).items():
        if not datasource_id.replace("_", "").isalnum():
            raise ValueError(f"Invalid datasource ID: {datasource_id}")
        allowed = frozenset(
            normalise_identifier(x) for x in item.get("allowed_tables", [])
        )
        if not allowed:
            raise ValueError(f"{datasource_id}: allowed_tables must not be empty")
        agents = frozenset(str(x) for x in item.get("allowed_agents", []))
        if not agents:
            raise ValueError(f"{datasource_id}: allowed_agents must not be empty")
        result[datasource_id] = DataSource(
            datasource_id=datasource_id,
            name=str(item.get("name", datasource_id)),
            dialect=str(item["dialect"]).casefold(),
            url_env=str(item["url_env"]),
            readonly_mode=str(item.get("readonly_mode", "unsupported")),
            allowed_tables=allowed,
            sensitive_columns=frozenset(
                str(x).casefold() for x in item.get("sensitive_columns", [])
            ),
            auto_execute=bool(item.get("auto_execute", False)),
            max_auto_scan_rows=int(item.get("max_auto_scan_rows", 0)),
            max_hard_scan_rows=int(item.get("max_hard_scan_rows", 0)),
            max_result_rows=int(item.get("max_result_rows", 100)),
            max_result_bytes=int(item.get("max_result_bytes", 524288)),
            timeout_ms=int(item.get("timeout_ms", 5000)),
            allowed_agents=agents,
        )
    return result
