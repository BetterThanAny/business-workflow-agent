# Operations runbook

## Start and migrate

```bash
docker compose up -d --build
mise exec -- uv sync --frozen
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
  mise exec -- uv run alembic upgrade head
```

Set `JWT_SECRET` through `.env` or `op run`; never put it in this file or shell
history. Confirm `docker compose ps` reports PostgreSQL and Redis healthy and
`alembic current` reports the recorded head before starting the API.

The default local mode uses the explicit deterministic knowledge stub. For the real
integration, set `KNOWLEDGE_BACKEND=enterprise_rag`, keep the service bearer token in
1Password, and configure an explicit JSON tenant mapping:

```bash
op run --env-file=.env -- mise exec -- uv run uvicorn \
  business_workflow_agent.app:create_app --factory --host 127.0.0.1 --port 8000
```

Never map a tenant implicitly. A missing mapping must remain a terminal configuration
error rather than falling back to another knowledge base.

## Serve and observe

```bash
mise exec -- uv run uvicorn business_workflow_agent.app:create_app \
  --factory --host 127.0.0.1 --port 8000
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/metrics
```

Investigate a run with an owner, same-tenant Admin, or same-tenant Auditor JWT:

```bash
curl --fail \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://127.0.0.1:8000/api/v1/agent-runs/${RUN_ID}/trajectory"
```

Read the merged evidence in timestamp order. Start with the final `state` and
`error_code`, then identify the last checkpoint, retry event, approval status, tool
status, and side-effect event. Never edit audit or side-effect event rows to recover a
run; use the documented resume/manual-review endpoints.

## Evaluation release gate

```bash
mise exec -- uv run python scripts/evaluate_agent.py \
  --dataset data/eval/agent_cases.jsonl \
  --report /tmp/business-workflow-agent-m5-report.json
```

Release only when the command exits zero, at least 150 cases ran, task success and
preferred-tool accuracy are each at least 90%, orchestration p95 is at most 200 ms,
and permission/duplicate counts are both zero. The report contains sample results and
full trajectories for failed cases.

## MCP stdio server

Create the workflow run through the authenticated API, then launch one server process
bound to its owner and run. Pass `MCP_ACCESS_TOKEN` and `MCP_RUN_ID` through an
isolated process environment; do not put either value in model-visible arguments.

```bash
op run --env-file=.env -- mise exec -- uv run python scripts/run_mcp_server.py
```

Verify the official SDK client/server path without a remote model:

```bash
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' KNOWLEDGE_BACKEND=deterministic_stub \
  mise exec -- uv run python scripts/smoke_mcp.py
```

## `llm-eval-platform`

Start this API, mint a short-lived Admin JWT, and export the complete header value:

```bash
export EVAL_AUTHORIZATION="Bearer ${SHORT_LIVED_ADMIN_TOKEN}"
```

Import `data/eval/agent_cases.jsonl` with the platform default JSONL mapping and use
`config/llm-eval-platform-target.json` as the HTTP target. Do not place the token in
the target JSON. The platform and this repository intentionally keep independent
Python runtimes; HTTP/JSON is the stable integration boundary.

## Opt-in live evidence

The default suite remains deterministic. Enable a local Ollama run explicitly and write the full
report under the ignored `artifacts/live-evidence/` directory:

```bash
PROVIDER_BACKEND=openai_compatible \
PROVIDER_BASE_URL=http://127.0.0.1:11434/v1 \
PROVIDER_MODEL=qwen2.5:0.5b \
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' \
  mise exec -- uv run python scripts/evaluate_live_agent.py \
  --manifest data/eval/live-v1.json \
  --report artifacts/live-evidence/ollama-live.json
```

For enterprise-rag, first create a disposable least-privilege identity, tenant, knowledge base and
known marker document in its clean local stack. Inject its short-lived token at runtime:

```bash
mise exec -- uv run python scripts/smoke_live_rag.py \
  --base-url http://127.0.0.1:8010 \
  --bearer-token "$ENTERPRISE_RAG_BEARER_TOKEN" \
  --tenant-id "$ENTERPRISE_RAG_TENANT_ID" \
  --knowledge-base-id "$ENTERPRISE_RAG_KNOWLEDGE_BASE_ID" \
  --query "authorized chunk smoke policy" \
  --expected-text "Enterprise smoke policy" \
  --enterprise-rag-repo ../enterprise-rag
```

The command must report a real HTTP hit, Redis cache replay, read-path fail-open with an unavailable
Redis endpoint, and a denied mismatched tenant. Missing live resources are `unverified`, not a
passing stub.

## Alerts

Alert on any increase in provider errors, tool denial/error outcomes, or p95 crossing
200 ms. A permission violation or duplicate side effect in an evaluation is a
zero-tolerance release block, not a warning.
