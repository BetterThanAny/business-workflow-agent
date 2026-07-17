# Verification evidence and limits

This document separates reproducible engineering evidence from claims the repository does not prove.
The public demo is a guided rendering of these deterministic scenarios; it is not connected to a
production backend.

## Evidence model

| Status | Meaning |
|---|---|
| `verified` | A named local/CI command exercised the stated behavior and passed. |
| `failed` | The behavior was exercised and did not meet the acceptance condition. |
| `unverified` | No suitable live environment, credential, or test has exercised the claim. |
| `non-finding` | An explicit audit found no skipped tests, zero-test run, open gate, or relevant gap. |

## Latest reproducible run

Recorded on 2026-07-17 after adding the public showcase and CI configuration:

| Surface | Result | Status |
|---|---|---|
| Test inventory / full suite with PostgreSQL and Redis gates enabled | 109 collected / 109 passed; no skipped or xfail | verified |
| Integration / security / fault suites | 29 / 19 / 8 passed | verified |
| Branch coverage | 92% overall; knowledge 94%; MCP adapter 96% | verified |
| Deterministic evaluation | 160/160; tool and argument accuracy 100% | verified |
| Authorization and duplicate-side-effect release gates | 0 violations; 0 duplicates | verified |
| Orchestration p95 | 19.746 ms in the local deterministic matrix | verified |
| Workflow smoke | approval, denial, cancellation, recovery, 10x replay | verified |
| MCP smoke | official SDK client/server, 8 tools, knowledge stub result | verified |
| Public showcase contract | 2 tests plus desktop/mobile browser interaction; zero console errors | verified |
| Secrets scan | no leaks found | verified |
| Skipped/xfail, zero tests, unresolved PostgreSQL/Redis gates | none found | non-finding |

The evaluation uses the real state machine, policy, schemas, persistence, approval and side-effect
paths, but it deliberately replaces nondeterministic external dependencies. Therefore `160/160`
means the deterministic safety and workflow contract is internally consistent; it is not an LLM
quality score.

The browser smoke used a local static server and headless Google Chrome at 1440×1000 and 390×844.
It switched among approval, injection and replay scenarios, advanced steps, checked mobile overflow
and asserted zero console errors. The first two browser-launch attempts did not exercise the page
because the Playwright Python module and bundled Chromium binary were absent; the successful run
explicitly used the already-installed local Chrome executable and required no global installation.

## Claim boundaries

| Claim | Status | Reason / required evidence |
|---|---|---|
| OpenAI-compatible provider interface and structured-output validation | verified | Unit tests use an injected transport and exercise malformed output/repair behavior. |
| Accuracy, latency and cost of a real hosted model | unverified | No paid or remote model is used by the default suite; requires a versioned opt-in live eval. |
| enterprise-rag HTTP contract, tenant headers and retry classification | verified | Deterministic HTTP transport tests assert exact requests, responses and failures. |
| Retrieval quality against a deployed enterprise-rag corpus | unverified | Requires a deployed service, service credential, tenant mapping and labelled corpus. |
| Redis isolation/cache/lease semantics | verified | Tests use the real Redis container, not an in-memory replacement. |
| Production deployment, remote telemetry and sustained load | unverified | No hosted API, telemetry backend, SLO run or load test is part of this repository. |

## Commands

```bash
docker compose up -d --build
mise exec -- uv sync --frozen
mise exec -- uv run ruff check .
mise exec -- uv run pyright
TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  mise exec -- uv run pytest --collect-only -q -ra
TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  mise exec -- uv run pytest -q -ra
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
  mise exec -- uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' KNOWLEDGE_BACKEND=deterministic_stub \
  mise exec -- uv run python scripts/smoke_workflow.py
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' KNOWLEDGE_BACKEND=deterministic_stub \
  mise exec -- uv run python scripts/smoke_mcp.py
mise exec -- uv run python scripts/evaluate_agent.py \
  --dataset data/eval/agent_cases.jsonl
gitleaks detect --source . --no-git --redact --exit-code 1
```

CI runs the reproducible default matrix. Live provider/RAG suites should remain opt-in so pull
requests neither spend money nor require business credentials.
