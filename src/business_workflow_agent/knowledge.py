from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from redis import Redis
from redis.exceptions import LockError, RedisError

from business_workflow_agent.schemas import (
    KnowledgeArticle,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
)
from business_workflow_agent.services import BusinessError
from business_workflow_agent.workflow.retry import RetryPolicy

if TYPE_CHECKING:
    from business_workflow_agent.config import Settings


class KnowledgeBackend(Protocol):
    name: str

    def search(
        self,
        *,
        tenant_id: UUID,
        data: SearchKnowledgeBaseInput,
    ) -> SearchKnowledgeBaseOutput: ...

    def close(self) -> None: ...


class KnowledgeIntegrationError(BusinessError):
    code = "KNOWLEDGE_INTEGRATION_ERROR"


class KnowledgeAuthorizationError(KnowledgeIntegrationError):
    code = "KNOWLEDGE_AUTHORIZATION_DENIED"


class KnowledgeNotFoundError(KnowledgeIntegrationError):
    code = "KNOWLEDGE_BASE_NOT_FOUND"


class KnowledgeRetryExhaustedError(KnowledgeIntegrationError):
    code = "KNOWLEDGE_RETRY_EXHAUSTED"


class KnowledgeResponseError(KnowledgeIntegrationError):
    code = "KNOWLEDGE_MALFORMED_RESPONSE"


class KnowledgeConfigurationError(KnowledgeIntegrationError):
    code = "KNOWLEDGE_TENANT_NOT_CONFIGURED"


_KNOWLEDGE_CATALOG = (
    KnowledgeArticle(
        article_id="kb-auth-001",
        title="Reset multi-factor authentication",
        excerpt="Verify the customer, revoke the old factor, then enroll a new factor.",
    ),
    KnowledgeArticle(
        article_id="kb-refund-001",
        title="Refund eligibility",
        excerpt="A refund cannot exceed the original purchase amount and requires authorization.",
    ),
    KnowledgeArticle(
        article_id="kb-ticket-001",
        title="Ticket priority guide",
        excerpt=(
            "Urgent priority is reserved for broad production impact or critical security events."
        ),
    ),
)


class DeterministicKnowledgeBackend:
    """Explicit local test double; never performs network or cache I/O."""

    name = "deterministic_knowledge_stub_v1"

    def search(
        self,
        *,
        tenant_id: UUID,
        data: SearchKnowledgeBaseInput,
    ) -> SearchKnowledgeBaseOutput:
        del tenant_id
        terms = set(data.query.casefold().split())
        ranked = sorted(
            _KNOWLEDGE_CATALOG,
            key=lambda article: sum(
                term in f"{article.title} {article.excerpt}".casefold() for term in terms
            ),
            reverse=True,
        )
        matched = [
            article
            for article in ranked
            if any(term in f"{article.title} {article.excerpt}".casefold() for term in terms)
        ]
        return SearchKnowledgeBaseOutput(articles=matched[: data.limit])

    def close(self) -> None:
        return None


class _RetrievalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    filename: str
    content: str
    page_number: int | None
    heading_path: str | None
    lexical_score: float | None
    dense_score: float | None
    fused_score: float | None
    rerank_score: float | None


class _RetrievalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: UUID
    mode: Literal["lexical", "dense", "hybrid"]
    retriever_version: str
    embedding_version: str
    reranker_version: str | None
    duration_ms: float
    results: list[_RetrievalCandidate]


class EnterpriseRagClient:
    """Strict client for enterprise-rag's stable tenant-scoped retrieval API."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 3,
        max_attempts: int = 2,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not bearer_token:
            raise ValueError("enterprise-rag bearer token must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.http_client = http_client or httpx.Client()
        self._owns_http_client = http_client is None
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.retry_policy = retry_policy or RetryPolicy(
            base_delay_seconds=0.1,
            max_delay_seconds=0.5,
            jitter_ratio=0.25,
        )
        self.sleeper = sleeper

    def search(
        self,
        *,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        data: SearchKnowledgeBaseInput,
    ) -> SearchKnowledgeBaseOutput:
        response: httpx.Response | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.http_client.post(
                    f"{self.base_url}/api/v1/knowledge-bases/{knowledge_base_id}/retrieve",
                    headers={
                        "Authorization": f"Bearer {self.bearer_token}",
                        "X-Tenant-ID": str(tenant_id),
                    },
                    json={
                        "query": data.query,
                        "mode": "hybrid",
                        "top_k": data.limit,
                        "candidate_k": data.limit * 4,
                        "rerank": False,
                    },
                    timeout=self.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_attempts:
                    raise KnowledgeRetryExhaustedError(
                        "enterprise-rag request failed after bounded retries"
                    ) from exc
                self.sleeper(self.retry_policy.delay_seconds(attempt))
                continue

            if response.status_code in {429} or response.status_code >= 500:
                if attempt == self.max_attempts:
                    raise KnowledgeRetryExhaustedError(
                        "enterprise-rag returned a retryable error after bounded retries"
                    )
                self.sleeper(self.retry_policy.delay_seconds(attempt))
                continue
            if response.status_code in {401, 403}:
                raise KnowledgeAuthorizationError("enterprise-rag denied the service identity")
            if response.status_code == 404:
                raise KnowledgeNotFoundError("enterprise-rag knowledge base was not found")
            if not 200 <= response.status_code < 300:
                raise KnowledgeIntegrationError(
                    f"enterprise-rag returned non-retryable HTTP {response.status_code}"
                )
            try:
                parsed = _RetrievalResponse.model_validate(response.json())
            except (ValidationError, ValueError) as exc:
                raise KnowledgeResponseError(
                    "enterprise-rag returned a malformed response"
                ) from exc
            return SearchKnowledgeBaseOutput(
                articles=[
                    KnowledgeArticle(
                        article_id=str(candidate.chunk_id),
                        title=candidate.heading_path or candidate.filename,
                        excerpt=candidate.content,
                    )
                    for candidate in parsed.results
                ]
            )
        raise KnowledgeRetryExhaustedError("enterprise-rag request did not produce a response")

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()


class RedisKnowledgeCache:
    """Tenant-keyed response cache plus token-owned Redis lease."""

    namespace = "business-workflow-agent:knowledge:v1"

    def __init__(
        self,
        client: Redis,
        *,
        ttl_seconds: int = 60,
        lease_seconds: int = 5,
    ) -> None:
        if ttl_seconds < 1 or lease_seconds < 1:
            raise ValueError("cache and lease TTLs must be positive")
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.lease_seconds = lease_seconds

    def cache_key(
        self,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        data: SearchKnowledgeBaseInput,
    ) -> str:
        canonical = json.dumps(
            {
                "tenant_id": str(tenant_id),
                "knowledge_base_id": str(knowledge_base_id),
                "query": data.query,
                "limit": data.limit,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return f"{self.namespace}:cache:{digest}"

    def get(self, key: str) -> SearchKnowledgeBaseOutput | None:
        try:
            raw = self.client.get(key)
        except RedisError:
            return None
        if raw is None:
            return None
        try:
            if not isinstance(raw, (str, bytes, bytearray)):
                return None
            return SearchKnowledgeBaseOutput.model_validate_json(raw)
        except ValidationError:
            with suppress(RedisError):
                self.client.delete(key)
            return None

    def set(self, key: str, value: SearchKnowledgeBaseOutput) -> None:
        try:
            self.client.set(key, value.model_dump_json(), ex=self.ttl_seconds)
        except RedisError:
            return None

    @contextmanager
    def lease(self, key: str) -> Generator[bool, None, None]:
        lock = self.client.lock(
            f"{self.namespace}:lease:{key}",
            timeout=self.lease_seconds,
        )
        acquired = False
        try:
            acquired = bool(lock.acquire(blocking=False))
        except RedisError:
            acquired = False
        try:
            yield acquired
        finally:
            if acquired:
                with suppress(LockError, RedisError):
                    lock.release()

    def close(self) -> None:
        self.client.close()


class EnterpriseRagKnowledgeBackend:
    name = "enterprise_rag_http_v1"

    def __init__(
        self,
        *,
        client: EnterpriseRagClient,
        cache: RedisKnowledgeCache,
        knowledge_base_ids: Mapping[UUID, UUID],
    ) -> None:
        self.client = client
        self.cache = cache
        self.knowledge_base_ids = dict(knowledge_base_ids)

    def search(
        self,
        *,
        tenant_id: UUID,
        data: SearchKnowledgeBaseInput,
    ) -> SearchKnowledgeBaseOutput:
        knowledge_base_id = self.knowledge_base_ids.get(tenant_id)
        if knowledge_base_id is None:
            raise KnowledgeConfigurationError(
                "no enterprise-rag knowledge base is configured for this tenant"
            )
        cache_key = self.cache.cache_key(tenant_id, knowledge_base_id, data)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        with self.cache.lease(cache_key):
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
            output = self.client.search(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                data=data,
            )
            self.cache.set(cache_key, output)
            return output

    def close(self) -> None:
        self.client.close()
        self.cache.close()


def create_knowledge_backend(settings: Settings) -> KnowledgeBackend:
    if settings.knowledge_backend == "deterministic_stub":
        return DeterministicKnowledgeBackend()
    token = settings.enterprise_rag_bearer_token
    if token is None or not settings.enterprise_rag_knowledge_base_ids:
        raise KnowledgeConfigurationError(
            "enterprise-rag mode requires a bearer token and tenant knowledge-base mapping"
        )
    redis_client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url,
        decode_responses=True,
    )
    return EnterpriseRagKnowledgeBackend(
        client=EnterpriseRagClient(
            base_url=settings.enterprise_rag_base_url,
            bearer_token=token.get_secret_value(),
            timeout_seconds=settings.enterprise_rag_timeout_seconds,
            max_attempts=settings.enterprise_rag_max_attempts,
        ),
        cache=RedisKnowledgeCache(
            redis_client,
            ttl_seconds=settings.knowledge_cache_ttl_seconds,
            lease_seconds=settings.knowledge_lock_ttl_seconds,
        ),
        knowledge_base_ids=settings.enterprise_rag_knowledge_base_ids,
    )
