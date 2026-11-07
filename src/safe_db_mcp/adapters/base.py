from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from safe_db_mcp.security.risk import QueryPlan


class DatabaseAdapter(ABC):
    @abstractmethod
    def verify_readonly(self) -> bool:
        """Verify native read-only transaction support on a fresh connection."""
        raise NotImplementedError

    @abstractmethod
    def explain(self, sql: str, parameters: dict[str, Any]) -> QueryPlan:
        """Return estimates only; must not execute the user query."""
        raise NotImplementedError

    @abstractmethod
    def execute_readonly(
        self, sql: str, parameters: dict[str, Any],
        max_rows: int, max_bytes: int, timeout_ms: int
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
