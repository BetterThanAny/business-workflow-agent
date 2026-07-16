import os
from collections.abc import Callable
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.db import create_session_factory
from business_workflow_agent.knowledge import (
    EnterpriseRagClient,
    EnterpriseRagKnowledgeBackend,
    RedisKnowledgeCache,
)
from business_workflow_agent.mcp_integration import (
    MCPExecutionContext,
    MCPToolClient,
    create_mcp_server,
)
from business_workflow_agent.models import Ticket
from business_workflow_agent.schemas import SearchKnowledgeBaseInput
from business_workflow_agent.tools.registry import build_tool_registry


def _rag_response() -> dict[str, object]:
    return {
        "trace_id": str(uuid4()),
        "mode": "hybrid",
        "retriever_version": "stub-retriever-v1",
        "embedding_version": "stub-embedding-v1",
        "reranker_version": None,
        "duration_ms": 1.0,
        "results": [
            {
                "rank": 1,
                "chunk_id": str(uuid4()),
                "document_id": str(uuid4()),
                "version_id": str(uuid4()),
                "filename": "policy.md",
                "content": "Reset MFA only after identity verification.",
                "page_number": 1,
                "heading_path": "MFA policy",
                "lexical_score": 1.0,
                "dense_score": 1.0,
                "fused_score": 1.0,
                "rerank_score": None,
            }
        ],
    }


def test_real_redis_cache_and_lock_wrap_enterprise_rag_http_stub() -> None:
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required for the Redis integration test")
    redis_client: Redis = Redis.from_url(redis_url, decode_responses=True)
    redis_client.flushdb()
    tenant_a = uuid4()
    tenant_b = uuid4()
    knowledge_base_ids = {tenant_a: uuid4(), tenant_b: uuid4()}
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_rag_response())

    backend = EnterpriseRagKnowledgeBackend(
        client=EnterpriseRagClient(
            base_url="https://rag.stub.test",
            bearer_token="test-service-token",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        cache=RedisKnowledgeCache(redis_client, ttl_seconds=60, lease_seconds=5),
        knowledge_base_ids=knowledge_base_ids,
    )
    query = SearchKnowledgeBaseInput(query="private customer MFA issue", limit=5)
    tenant_a_cache_key = backend.cache.cache_key(
        tenant_a,
        knowledge_base_ids[tenant_a],
        query,
    )
    redis_client.set(tenant_a_cache_key, "malformed-cache-entry")

    first = backend.search(tenant_id=tenant_a, data=query)
    second = backend.search(tenant_id=tenant_a, data=query)
    cross_tenant = backend.search(tenant_id=tenant_b, data=query)

    assert first == second
    assert cross_tenant.articles
    assert calls == 2
    keys = [str(key) for key in redis_client.scan_iter(match="business-workflow-agent:*")]
    assert keys
    assert all(query.query not in key for key in keys)

    cache = backend.cache
    with (
        cache.lease("exclusive-test") as first_lease,
        cache.lease("exclusive-test") as second_lease,
    ):
        assert first_lease is True
        assert second_lease is False
    redis_client.close()


def test_mcp_sdk_client_lists_exact_schemas_and_replays_write_once(
    engine: object,
    session: Session,
    principal_factory: Callable[..., Principal],
    create_run: Callable[[Principal], UUID],
    create_customer: Callable[[Principal, str], dict[str, object]],
) -> None:
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)
    customer = create_customer(admin, "mcp")
    run_id = create_run(support)
    registry = build_tool_registry()
    server = create_mcp_server(
        create_session_factory(engine),
        registry,
        MCPExecutionContext(principal=support, run_id=run_id),
    )

    async def exercise() -> None:
        async with create_connected_server_and_client_session(server) as sdk_session:
            client = MCPToolClient(sdk_session)
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == set(registry.names())
            search_schema = next(
                tool.inputSchema for tool in tools if tool.name == "search_knowledge_base"
            )
            assert search_schema == registry.get(
                "search_knowledge_base"
            ).input_model.model_json_schema()

            malformed = await sdk_session.call_tool(
                "search_knowledge_base",
                {"query": "MFA", "limit": 5, "role": "ADMIN"},
            )
            assert malformed.isError is True

            arguments = {
                "customer_id": str(customer["id"]),
                "title": "MCP-created ticket",
                "description": "Created through the authenticated MCP execution context",
                "priority": "NORMAL",
            }
            first = await client.call_tool("create_ticket", arguments)
            replay = await client.call_tool("create_ticket", arguments)
            assert first.status == "SUCCEEDED"
            assert replay.status == "SUCCEEDED"
            assert replay.replayed is True
            assert first.tool_call_id == replay.tool_call_id

    anyio.run(exercise)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Ticket)) == 1
