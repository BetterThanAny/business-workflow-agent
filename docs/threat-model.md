# Threat model

## Protected assets

- tenant business objects and side effects;
- approval authority and one-time decision tokens;
- raw model/tool inputs that may contain PII;
- append-only checkpoint, audit, and side-effect evidence;
- evaluation datasets and release-gate results.

## Primary threats and controls

| Threat | Control | Deterministic evidence |
|---|---|---|
| Prompt or retrieved-text injection grants a role | Executor uses signed server identity and server role grants | security injection suite; 20 M5 injection cases |
| Model selects an unknown or mismatched tool | Fixed registry and intent/tool binding | agent boundary tests |
| Cross-tenant trajectory disclosure | Tenant predicate plus owner/Admin/Auditor check; foreign access is 404 | M5 trajectory integration test |
| PII leaks through trajectory or telemetry | Persisted redaction; bounded non-content span attributes and metric labels | M5 redaction assertion and existing audit tests |
| Replay duplicates a write | Unique idempotency constraints and durable outbox | 20 replay cases, each replayed ten times |
| REST or Agent high-risk write skips approval | Shared persisted approval path, explicit origin and independent decision identity | direct/agent approval, replay and security suites |
| Provider timeout loses progress | Typed retry classification, durable next-retry timestamp, bounded backoff | 20 timeout/recovery cases |
| Evaluation endpoint mutates production | Admin authentication plus isolated per-case in-memory database | HTTP target contract test |
| Evaluation hides failures | Explicit denominators, zero-tolerance permission/duplicate gates, full failed trajectories | evaluator unit test and report artifact |
| MCP arguments forge tenant, role, or approval | Identity and run are fixed in the server execution context; exact schemas reject extra fields | M6 MCP security test |
| MCP replay duplicates a write | Server-derived idempotency key plus existing database uniqueness/outbox | M6 SDK client replay test |
| Knowledge query crosses tenants | Server maps authenticated tenant to a fixed knowledge-base ID; downstream API rechecks membership | M6 cross-tenant cache test and RAG request assertions |
| Redis key leaks a sensitive query | Tenant-scoped canonical payload is SHA-256 hashed before key construction | M6 real Redis key inspection |
| Retrieved response changes schema | Strict local response model rejects missing, extra, or malformed fields | M6 malformed-response test |

## Residual risks

- `/metrics` is intentionally unauthenticated for Prometheus scraping. It contains no
  PII or tenant labels, but production deployment must restrict it at the network or
  reverse-proxy layer.
- The default OpenTelemetry provider has no remote exporter. Production must attach
  a vetted exporter and apply retention/access controls outside this repository.
- The deterministic suite proves orchestration and policy behavior, not the quality
  or latency of an opt-in live model. Live-provider evaluation remains separate and
  must never become a release substitute for deterministic authorization assertions.
- The live provider and RAG commands are opt-in local evidence. Their absence, failure,
  or low quality must remain visible and must not be replaced by stub results.
- Redis is fail-open for knowledge reads. An outage can increase load on
  `enterprise-rag`; production rate limits and monitoring remain required.
- The stdio MCP server receives a short-lived business JWT through process
  environment. The launcher must isolate child processes and avoid environment dumps.
