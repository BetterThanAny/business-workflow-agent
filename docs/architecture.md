# Architecture

## Trust boundary

The model-facing provider can only return a structured proposal. `AgentRunner`
persists explicit transitions and sends a registered proposal to `ToolExecutor`.
The executor re-validates the fixed Pydantic schema and authorizes the authenticated
server-side `Principal`; model text, context, retrieved text, and proposed arguments
never become authority.

```text
client/JWT -> FastAPI -> AgentRunner -> StructuredProvider (untrusted proposal)
                         |       |
                         |       +-> redacted workflow/LLM spans and metrics
                         v
                    ToolExecutor -> Policy/RBAC -> outbox -> fixed handler
                         |                          |
                         +-> approval pause         +-> idempotent side effect
                         |
                         +-> append-only audit, checkpoint, event, side-effect event
```

Core domain, policy, tool schemas, and execution are framework-independent. The
OpenTelemetry and Prometheus integration is injected through `WorkflowTelemetry`, so
exporters can change without changing authorization or state semantics.

## MCP and knowledge boundary

The MCP Python SDK adapter exports the same fixed Pydantic input schemas as the REST
API. A server process is bound to a trusted Principal and run ID before any tool call;
MCP arguments contain business inputs only. The adapter derives idempotency keys and
invokes `ToolExecutor`, so MCP cannot bypass schema, role/scope, approval, audit, or
outbox controls.

```text
MCP Client -> SDK Server (trusted Principal + run ID) -> ToolExecutor
                                                     -> enterprise-rag HTTP
                                                        |       |
                                                        |       +-> tenant ACL
                                                        +-> Redis cache/lease
```

`search_knowledge_base` resolves an `enterprise-rag` knowledge-base ID from a
server-side tenant mapping. Redis keys contain only a namespace and SHA-256 digest;
raw query text and bearer tokens are never persisted. Redis is advisory for reads,
while the downstream API remains the evidence and authorization source.

## Evaluation boundary

`data/eval/agent_cases.jsonl` is a versioned 160-case snapshot. The local evaluator
uses the real state machine, policy, registry, outbox, and persistence code with the
deterministic provider and a fresh in-memory database per case. Timeout cases inject
a typed provider timeout, advance the deterministic clock, and resume from the
persisted retry checkpoint. Replay cases call the terminal workflow ten times and
assert one business side effect.

`POST /api/v1/evaluation/target` is the `llm-eval-platform` HTTP target. It requires
an Admin JWT, accepts the platform's `input` envelope, and evaluates only inside an
isolated in-memory database. It returns the platform agent-evaluator fields and a
redacted full trajectory. It does not access the application's production database.

## Evidence surfaces

- `GET /api/v1/agent-runs/{run_id}/trajectory` merges checkpoints, workflow events,
  approvals, tool calls, side-effect events, audit events, and error codes. Only
  redacted fields are returned; cross-tenant access returns 404.
- `GET /metrics` exports bounded-label workflow, provider, tool, and orchestration
  Prometheus series. No message, argument, user, tenant, or run ID is a metric label.
- OpenTelemetry spans are named `workflow.run`, `workflow.step`, `llm.classify`,
  `llm.repair`, `llm.summarize`, and `tool.execute`. Attributes contain stable IDs,
  states, operation names, and tool names, but no model text or tool arguments.
