"""Authenticated MCP client setup and capability invocation for the Agent Runtime."""

from __future__ import annotations

import json
import logging
import os
import time
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from services.agent_runtime.observability import current_trace_id

logger = logging.getLogger("oraiflow.agent_runtime.mcp")


class MCPToolExecutionError(RuntimeError):
    """An MCP server returned ``isError=True`` for a successfully dispatched call.

    This is deliberately distinct from connection/initialization failures:
    retrying a rejected SQL/tool invocation as a transport outage can amplify
    deterministic server-side errors and obscure the actual failure boundary.
    """

    def __init__(self, tool_name: str):
        self.tool_name = str(tool_name)
        super().__init__(f"MCP tool execution failed: {self.tool_name}")


async def call_banking_tool(name: str, arguments: dict | None = None) -> str:
    """Call the mandatory MCP boundary over Streamable HTTP, even locally."""
    url = os.getenv("MCP_URL", "http://mcp:8100/mcp")
    token = os.getenv("MCP_ACCESS_TOKEN", "local-mcp-token-change-me")
    headers = {"Authorization": f"Bearer {token}", "X-ORAI-Trace-ID": current_trace_id()}
    started_at = time.perf_counter()
    outcome = "error"
    logger.info("mcp_call_started trace_id=%s tool=%s", current_trace_id(), name)
    try:
        async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})
                if getattr(result, "isError", False):
                    outcome = "tool_error"
                    raise MCPToolExecutionError(name)
                chunks = [part.text for part in (result.content or []) if hasattr(part, "text")]
                outcome = "ok"
                return "\n".join(chunks) if chunks else json.dumps({"result": "ok"})
    finally:
        logger.info(
            "mcp_call_finished trace_id=%s tool=%s outcome=%s duration_ms=%d",
            current_trace_id(),
            name,
            outcome,
            round((time.perf_counter() - started_at) * 1000),
        )
