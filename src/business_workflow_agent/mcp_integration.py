from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mcp import types
from mcp.client.session import ClientSession
from mcp.server.lowlevel import Server
from sqlalchemy.orm import Session, sessionmaker

from business_workflow_agent.auth import Principal
from business_workflow_agent.execution import ToolExecutor
from business_workflow_agent.schemas import ToolExecutionResponse
from business_workflow_agent.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class MCPExecutionContext:
    """Trusted process context; none of these fields come from model tool arguments."""

    principal: Principal
    run_id: UUID


class MCPToolProtocolError(RuntimeError):
    pass


def _idempotency_key(run_id: UUID, tool_name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return f"mcp:{run_id}:{tool_name}:{digest}"


def create_mcp_server(
    session_factory: sessionmaker[Session],
    registry: ToolRegistry,
    context: MCPExecutionContext,
) -> Server[Any]:
    server: Server[Any] = Server(
        "business-workflow-agent",
        version="0.1.0",
        instructions=(
            "Fixed-schema business tools. Identity, run ownership, authorization, approval, "
            "and idempotency are enforced by the server-side execution context."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=definition.name,
                description=(
                    f"{definition.risk.value} business tool; requires "
                    f"scope {definition.required_scope}."
                ),
                inputSchema=definition.input_model.model_json_schema(),
                outputSchema=ToolExecutionResponse.model_json_schema(),
            )
            for definition in (registry.get(name) for name in registry.names())
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        definition = registry.get(name)
        key = (
            _idempotency_key(context.run_id, name, arguments)
            if definition.idempotency_required
            else None
        )
        with session_factory() as session:
            result = ToolExecutor(session, registry).execute(
                tool_name=name,
                arguments=arguments,
                principal=context.principal,
                run_id=context.run_id,
                idempotency_key=key,
            )
        return result.model_dump(mode="json")

    return server


class MCPToolClient:
    """Typed wrapper around the official MCP ClientSession."""

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def list_tools(self) -> list[types.Tool]:
        return (await self.session.list_tools()).tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResponse:
        result = await self.session.call_tool(name, arguments)
        if result.isError or result.structuredContent is None:
            raise MCPToolProtocolError(f"MCP tool call failed: {name}")
        return ToolExecutionResponse.model_validate(result.structuredContent)
