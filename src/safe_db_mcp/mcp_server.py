from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("safe-db-mcp")


def _client() -> httpx.AsyncClient:
    token = os.getenv("SAFEDB_AGENT_TOKEN", "")
    if not token:
        raise RuntimeError("SAFEDB_AGENT_TOKEN must be set in the MCP host environment")
    return httpx.AsyncClient(
        base_url=os.getenv("SAFEDB_API_URL", "http://127.0.0.1:8080"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


@mcp.tool()
async def list_datasources() -> dict[str, Any]:
    """List logical datasource IDs. No database connection details are returned."""
    async with _client() as client:
        response = await client.get("/v1/datasources")
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def prepare_query(
    datasource_id: str,
    sql: str,
    purpose: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit a SELECT query for AST validation, risk analysis and conditional approval."""
    async with _client() as client:
        response = await client.post("/v1/queries", json={
            "datasource_id": datasource_id,
            "sql": sql,
            "purpose": purpose,
            "parameters": parameters or {},
        })
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_query_status(proposal_id: str) -> dict[str, Any]:
    """Get the status and risk summary for a proposal submitted by this Agent."""
    async with _client() as client:
        response = await client.get(f"/v1/queries/{proposal_id}")
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_query_result(proposal_id: str) -> dict[str, Any]:
    """Get bounded rows for a successfully executed query owned by this Agent."""
    async with _client() as client:
        response = await client.get(f"/v1/queries/{proposal_id}/result")
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    mcp.run()
