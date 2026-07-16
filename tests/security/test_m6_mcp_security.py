from collections.abc import Callable
from uuid import UUID

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.db import create_session_factory
from business_workflow_agent.mcp_integration import MCPExecutionContext, create_mcp_server
from business_workflow_agent.tools.registry import build_tool_registry


def test_mcp_arguments_cannot_supply_identity_or_bypass_server_principal(
    engine: object,
    principal_factory: Callable[..., Principal],
    create_run: Callable[[Principal], UUID],
) -> None:
    restricted_support = principal_factory(
        Role.SUPPORT_AGENT,
        frozenset({"workflow:create"}),
    )
    run_id = create_run(restricted_support)
    server = create_mcp_server(
        create_session_factory(engine),
        build_tool_registry(),
        MCPExecutionContext(principal=restricted_support, run_id=run_id),
    )

    async def exercise() -> None:
        async with create_connected_server_and_client_session(server) as client:
            injected = await client.call_tool(
                "search_knowledge_base",
                {
                    "query": "MFA",
                    "limit": 5,
                    "tenant_id": str(restricted_support.tenant_id),
                    "roles": ["ADMIN"],
                },
            )
            assert injected.isError is True

            denied = await client.call_tool(
                "search_knowledge_base",
                {"query": "MFA", "limit": 5},
            )
            assert denied.isError is False
            assert denied.structuredContent is not None
            assert denied.structuredContent["status"] == "DENIED"
            assert denied.structuredContent["error"] in {
                "AUTHORIZATION_DENY_MISSING_SCOPE",
            }

    anyio.run(exercise)
