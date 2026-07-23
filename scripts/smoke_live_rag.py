import argparse
import json
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

from redis import Redis

from business_workflow_agent.knowledge import (
    EnterpriseRagClient,
    EnterpriseRagKnowledgeBackend,
    KnowledgeAuthorizationError,
    KnowledgeNotFoundError,
    RedisKnowledgeCache,
)
from business_workflow_agent.schemas import SearchKnowledgeBaseInput


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _backend(
    *,
    base_url: str,
    token: str,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    redis_url: str,
) -> EnterpriseRagKnowledgeBackend:
    return EnterpriseRagKnowledgeBackend(
        client=EnterpriseRagClient(
            base_url=base_url,
            bearer_token=token,
            timeout_seconds=10,
            max_attempts=2,
        ),
        cache=RedisKnowledgeCache(
            Redis.from_url(redis_url, decode_responses=True),
            ttl_seconds=60,
            lease_seconds=5,
        ),
        knowledge_base_ids={tenant_id: knowledge_base_id},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the real enterprise-rag HTTP boundary.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--bearer-token", required=True)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--knowledge-base-id", type=UUID, required=True)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:56379/0")
    parser.add_argument("--query", required=True)
    parser.add_argument("--expected-text", required=True)
    parser.add_argument("--enterprise-rag-repo", type=Path, required=True)
    args = parser.parse_args()
    query = SearchKnowledgeBaseInput(query=args.query, limit=5)

    live = _backend(
        base_url=args.base_url,
        token=args.bearer_token,
        tenant_id=args.tenant_id,
        knowledge_base_id=args.knowledge_base_id,
        redis_url=args.redis_url,
    )
    try:
        first = live.search(tenant_id=args.tenant_id, data=query)
    finally:
        live.close()
    if not any(args.expected_text in article.excerpt for article in first.articles):
        raise SystemExit("enterprise-rag result did not contain the expected marker")

    cached = _backend(
        base_url="http://127.0.0.1:1",
        token=args.bearer_token,
        tenant_id=args.tenant_id,
        knowledge_base_id=args.knowledge_base_id,
        redis_url=args.redis_url,
    )
    try:
        second = cached.search(tenant_id=args.tenant_id, data=query)
    finally:
        cached.close()
    if second != first:
        raise SystemExit("Redis cache replay changed the enterprise-rag result")

    redis_outage = _backend(
        base_url=args.base_url,
        token=args.bearer_token,
        tenant_id=args.tenant_id,
        knowledge_base_id=args.knowledge_base_id,
        redis_url="redis://127.0.0.1:1/0",
    )
    try:
        fail_open = redis_outage.search(tenant_id=args.tenant_id, data=query)
    finally:
        redis_outage.close()
    if fail_open != first:
        raise SystemExit("Redis outage fail-open changed the enterprise-rag result")

    client = EnterpriseRagClient(
        base_url=args.base_url,
        bearer_token=args.bearer_token,
        timeout_seconds=10,
        max_attempts=1,
    )
    wrong_tenant_denied = False
    try:
        client.search(
            tenant_id=uuid4(),
            knowledge_base_id=args.knowledge_base_id,
            data=query,
        )
    except (KnowledgeAuthorizationError, KnowledgeNotFoundError):
        wrong_tenant_denied = True
    finally:
        client.close()
    if not wrong_tenant_denied:
        raise SystemExit("enterprise-rag accepted a mismatched tenant header")

    print(
        json.dumps(
            {
                "status": "ok",
                "article_count": len(first.articles),
                "cache_hit_verified": True,
                "redis_outage_fail_open": True,
                "wrong_tenant_denied": True,
                "enterprise_rag_sha": _head(args.enterprise_rag_repo),
                "business_workflow_agent_sha": _head(Path.cwd()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
