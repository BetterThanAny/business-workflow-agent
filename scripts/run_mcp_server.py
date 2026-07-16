from __future__ import annotations

import os
from uuid import UUID

import anyio
from mcp.server.stdio import stdio_server

from business_workflow_agent.auth import decode_access_token
from business_workflow_agent.config import Settings
from business_workflow_agent.db import create_database_engine, create_session_factory
from business_workflow_agent.knowledge import create_knowledge_backend
from business_workflow_agent.mcp_integration import MCPExecutionContext, create_mcp_server
from business_workflow_agent.tools.registry import build_tool_registry


async def serve() -> None:
    settings = Settings()  # type: ignore[call-arg]
    raw_token = os.environ.get("MCP_ACCESS_TOKEN")
    raw_run_id = os.environ.get("MCP_RUN_ID")
    if not raw_token or not raw_run_id:
        raise RuntimeError("MCP_ACCESS_TOKEN and MCP_RUN_ID are required")
    principal = decode_access_token(
        raw_token,
        secret=settings.jwt_secret.get_secret_value(),
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    run_id = UUID(raw_run_id)
    engine = create_database_engine(settings.database_url)
    backend = create_knowledge_backend(settings)
    server = create_mcp_server(
        create_session_factory(engine),
        build_tool_registry(knowledge_backend=backend),
        MCPExecutionContext(principal=principal, run_id=run_id),
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        backend.close()
        engine.dispose()


if __name__ == "__main__":
    anyio.run(serve)
