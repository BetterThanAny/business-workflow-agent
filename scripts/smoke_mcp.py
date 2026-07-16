from __future__ import annotations

import json
import os
import sys
from uuid import uuid4

import anyio
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

from business_workflow_agent.auth import (
    Principal,
    Role,
    create_access_token,
    granted_scopes,
)
from business_workflow_agent.config import Settings
from business_workflow_agent.db import create_database_engine, create_session_factory
from business_workflow_agent.mcp_integration import MCPToolClient
from business_workflow_agent.schemas import WorkflowBudget
from business_workflow_agent.services import BusinessService


async def smoke() -> None:
    settings = Settings()  # type: ignore[call-arg]
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    roles = frozenset({Role.SUPPORT_AGENT})
    principal = Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        roles=roles,
        scopes=granted_scopes(roles),
    )
    with factory.begin() as session:
        run = BusinessService(session).create_workflow_run(principal, WorkflowBudget())
        run_id = run.id
    token = create_access_token(
        principal,
        secret=settings.jwt_secret.get_secret_value(),
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    child_env = {
        **os.environ,
        "MCP_ACCESS_TOKEN": token,
        "MCP_RUN_ID": str(run_id),
        "KNOWLEDGE_BACKEND": "deterministic_stub",
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["scripts/run_mcp_server.py"],
        env=child_env,
    )
    try:
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            client = MCPToolClient(session)
            tools = await client.list_tools()
            result = await client.call_tool(
                "search_knowledge_base",
                {"query": "MFA reset", "limit": 5},
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "transport": "stdio",
                    "tool_count": len(tools),
                    "knowledge_status": result.status,
                    "knowledge_result_count": len((result.result or {}).get("articles", [])),
                },
                sort_keys=True,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    anyio.run(smoke)
