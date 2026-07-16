# ADR 0006: MCP execution context and tenant-scoped knowledge integration

## Status

Accepted for M6 on 2026-07-16.

## Decision

1. Keep the fixed Pydantic tool registry as the source of MCP input schemas. The MCP
   SDK server exports those schemas and routes calls through `ToolExecutor`; it does
   not dynamically import handlers or execute model-selected code.
2. Bind each MCP server process to an authenticated `Principal` and existing run ID.
   These values are decoded or selected before the model can call a tool and never
   appear in MCP tool arguments. Required idempotency keys are derived server-side
   from the run, tool name, and canonical validated arguments.
3. Call `enterprise-rag` through its versioned HTTP retrieval endpoint. Forward a
   server-held service bearer token and the authenticated business tenant ID. Resolve
   the knowledge-base ID from an explicit server-side tenant mapping; never let model
   arguments choose a tenant or knowledge base.
4. Use Redis only for response caching and short token-owned cache-fill leases. Cache
   keys hash the tenant, knowledge-base ID, query, and limit so raw query text is not
   exposed in key listings. PostgreSQL and downstream services remain authoritative.
5. Fail open around Redis for read availability, but fail closed for missing tenant
   mappings, downstream authorization failures, malformed responses, and exhausted
   bounded retries.
6. Keep the deterministic local knowledge backend as an explicit default test double.
   Exercise the production HTTP adapter with a strict RAG stub and the cache/lease
   behavior with a real Redis container.

## Consequences

MCP is a replaceable protocol adapter rather than a new authorization boundary. Redis
loss can increase downstream read load but cannot grant permissions or corrupt source
of truth. Production deployment must provision a short-lived `enterprise-rag` service
identity that is a member only of the mapped tenants and rotate it outside this repo.
