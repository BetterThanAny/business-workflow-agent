import json
from uuid import uuid4

import httpx
import pytest
from redis import Redis

from business_workflow_agent.config import Settings
from business_workflow_agent.knowledge import (
    DeterministicKnowledgeBackend,
    EnterpriseRagClient,
    EnterpriseRagKnowledgeBackend,
    KnowledgeConfigurationError,
    KnowledgeIntegrationError,
    KnowledgeResponseError,
    KnowledgeRetryExhaustedError,
    RedisKnowledgeCache,
    create_knowledge_backend,
)
from business_workflow_agent.schemas import SearchKnowledgeBaseInput
from business_workflow_agent.workflow.retry import RetryPolicy


def _retrieval_response() -> dict[str, object]:
    return {
        "trace_id": str(uuid4()),
        "mode": "hybrid",
        "retriever_version": "postgres-fts-pgvector-rrf-v1",
        "embedding_version": "deterministic-sha256-v1",
        "reranker_version": None,
        "duration_ms": 4.5,
        "results": [
            {
                "rank": 1,
                "chunk_id": str(uuid4()),
                "document_id": str(uuid4()),
                "version_id": str(uuid4()),
                "filename": "mfa-runbook.md",
                "content": "Verify the customer before resetting MFA.",
                "page_number": 2,
                "heading_path": "Authentication / MFA",
                "lexical_score": 0.8,
                "dense_score": 0.9,
                "fused_score": 0.85,
                "rerank_score": None,
            }
        ],
    }


def test_enterprise_rag_client_sends_tenant_auth_and_retries_429() -> None:
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(429, json={"detail": "rate limited"}),
        httpx.Response(200, json=_retrieval_response()),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    client = EnterpriseRagClient(
        base_url="https://rag.example.test",
        bearer_token="test-service-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=1,
        max_attempts=2,
        retry_policy=RetryPolicy(
            base_delay_seconds=0.01,
            max_delay_seconds=0.01,
            jitter_ratio=0,
        ),
        sleeper=lambda _delay: None,
    )

    output = client.search(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        data=SearchKnowledgeBaseInput(query="reset MFA", limit=3),
    )

    assert len(requests) == 2
    assert requests[0].url.path == f"/api/v1/knowledge-bases/{knowledge_base_id}/retrieve"
    assert requests[0].headers["X-Tenant-ID"] == str(tenant_id)
    assert requests[0].headers["Authorization"] == "Bearer test-service-token"
    assert json.loads(requests[0].content) == {
        "query": "reset MFA",
        "mode": "hybrid",
        "top_k": 3,
        "candidate_k": 12,
        "rerank": False,
    }
    assert output.articles[0].title == "Authentication / MFA"
    assert output.articles[0].excerpt == "Verify the customer before resetting MFA."


def test_enterprise_rag_client_rejects_malformed_success_response() -> None:
    client = EnterpriseRagClient(
        base_url="https://rag.example.test",
        bearer_token="test-service-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"results": "not-a-list"})
            )
        ),
        sleeper=lambda _delay: None,
    )

    with pytest.raises(KnowledgeResponseError, match="malformed"):
        client.search(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            data=SearchKnowledgeBaseInput(query="MFA", limit=5),
        )


@pytest.mark.parametrize(
    ("bearer_token", "max_attempts", "message"),
    [("", 2, "token"), ("test-service-token", 0, "attempt")],
)
def test_enterprise_rag_client_rejects_invalid_configuration(
    bearer_token: str,
    max_attempts: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        EnterpriseRagClient(
            base_url="https://rag.example.test",
            bearer_token=bearer_token,
            max_attempts=max_attempts,
        )


@pytest.mark.parametrize("retryable_failure", ["timeout", "5xx"])
def test_enterprise_rag_client_retries_timeout_and_5xx(
    retryable_failure: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if retryable_failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(200, json=_retrieval_response())

    client = EnterpriseRagClient(
        base_url="https://rag.example.test",
        bearer_token="test-service-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        sleeper=lambda _delay: None,
    )

    assert client.search(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        data=SearchKnowledgeBaseInput(query="MFA", limit=5),
    ).articles
    assert calls == 2


def test_enterprise_rag_retry_exhaustion_is_distinct() -> None:
    client = EnterpriseRagClient(
        base_url="https://rag.example.test",
        bearer_token="test-service-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503, json={"detail": "unavailable"})
            )
        ),
        max_attempts=2,
        sleeper=lambda _delay: None,
    )

    with pytest.raises(KnowledgeRetryExhaustedError):
        client.search(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            data=SearchKnowledgeBaseInput(query="MFA", limit=5),
        )


def test_missing_tenant_mapping_fails_before_network_or_redis_io() -> None:
    backend = EnterpriseRagKnowledgeBackend(
        client=EnterpriseRagClient(
            base_url="https://rag.example.test",
            bearer_token="test-service-token",
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: pytest.fail("network must not be called")
                )
            ),
        ),
        cache=RedisKnowledgeCache(
            Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True)
        ),
        knowledge_base_ids={},
    )

    with pytest.raises(KnowledgeConfigurationError, match="configured"):
        backend.search(
            tenant_id=uuid4(),
            data=SearchKnowledgeBaseInput(query="MFA", limit=5),
        )


def test_redis_outage_fails_open_to_enterprise_rag_read_path() -> None:
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_retrieval_response())

    backend = EnterpriseRagKnowledgeBackend(
        client=EnterpriseRagClient(
            base_url="https://rag.example.test",
            bearer_token="test-service-token",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        cache=RedisKnowledgeCache(
            Redis.from_url(
                "redis://127.0.0.1:1/0",
                decode_responses=True,
                socket_connect_timeout=0.01,
                socket_timeout=0.01,
            )
        ),
        knowledge_base_ids={tenant_id: knowledge_base_id},
    )

    output = backend.search(
        tenant_id=tenant_id,
        data=SearchKnowledgeBaseInput(query="MFA", limit=5),
    )
    assert output.articles
    assert calls == 1


def test_backend_factory_requires_explicit_production_mapping_and_token() -> None:
    base = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "jwt_secret": "test-only-secret-with-more-than-thirty-two-bytes",
        "knowledge_backend": "enterprise_rag",
    }
    with pytest.raises(KnowledgeConfigurationError, match="requires"):
        create_knowledge_backend(Settings(**base))

    tenant_id = uuid4()
    backend = create_knowledge_backend(
        Settings(
            **base,
            enterprise_rag_bearer_token="test-service-token",
            enterprise_rag_knowledge_base_ids={tenant_id: uuid4()},
            redis_url="redis://127.0.0.1:1/0",
        )
    )
    assert isinstance(backend, EnterpriseRagKnowledgeBackend)
    backend.close()


def test_deterministic_backend_is_explicit_and_tenant_mapping_is_not_guessed() -> None:
    backend = DeterministicKnowledgeBackend()
    output = backend.search(
        tenant_id=uuid4(),
        data=SearchKnowledgeBaseInput(query="refund authorization", limit=2),
    )

    assert backend.name == "deterministic_knowledge_stub_v1"
    assert [article.article_id for article in output.articles] == ["kb-refund-001"]


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_enterprise_rag_client_does_not_retry_non_retryable_statuses(
    status_code: int,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json={"detail": "denied"})

    client = EnterpriseRagClient(
        base_url="https://rag.example.test",
        bearer_token="test-service-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=3,
        sleeper=lambda _delay: None,
    )

    with pytest.raises(KnowledgeIntegrationError):
        client.search(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            data=SearchKnowledgeBaseInput(query="MFA", limit=5),
        )
    assert calls == 1
