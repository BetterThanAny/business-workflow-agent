# Business Workflow Agent

面向企业客服与 IT 支持场景的安全业务后端示例。当前实现范围以
[`PLAN.md`](PLAN.md) 中的里程碑状态为准。

项目坚持由服务端完成授权、状态、幂等和审计；模型输出始终作为不可信输入。

## 本地运行

```bash
cp .env.example .env
# 将 JWT_SECRET 替换为本机生成的随机值
docker compose up -d
mise exec -- uv sync
mise exec -- uv run alembic upgrade head
mise exec -- uv run uvicorn business_workflow_agent.app:create_app --factory
```

API 使用服务端签名的 JWT，角色只授予固定 scope 集合。写请求还需要
`X-Workflow-Run-ID` 和 `Idempotency-Key`。工具 schema 可从
`GET /api/v1/tools/schemas` 导出。

默认测试不调用付费或远程模型；知识库使用明确标记的确定性内存测试 stub。

## M2 Agent 工作流

`POST /api/v1/agent-runs` 接收消息、可信业务上下文和预算，运行到
`COMPLETE`、`CLARIFY`、`AWAIT_APPROVAL` 或 `MANUAL_REVIEW` 等持久化停点。
每次状态迁移都在同一数据库事务内增加 run version 并写入 checkpoint。

- `GET /api/v1/agent-runs/{run_id}`：读取当前持久化状态。
- `POST /api/v1/agent-runs/{run_id}/resume`：只恢复等待补充参数的流程。
- `GET /api/v1/agent-runs/{run_id}/events`：读取脱敏后的 SSE 事件。

默认 `DeterministicProvider` 是明确标记的本地测试 stub。生产接入点是注入式
`OpenAICompatibleProvider`；模型只能返回结构化提案，工具名仍由固定注册表和
服务端 intent 映射校验，授权始终在工具执行器内完成。

## M3 人工审批

高风险工具只能先创建持久化 approval 并暂停流程。发起者不能批准自己的请求；另一名
具备审批权限且仍被授权执行目标工具的用户，需要领取短期、身份绑定的一次性 token，
再批准或拒绝。token 只返回一次，数据库仅保存哈希；拒绝、过期或已使用的审批不能恢复
原工具调用。

- `GET /api/v1/approvals/{approval_id}`：读取审批及脱敏后的工具参数。
- `POST /api/v1/approvals/{approval_id}/decision-token`：为当前独立审批人签发一次性 token。
- `POST /api/v1/approvals/{approval_id}/decision`：使用 token 批准或拒绝。

审批 API 不接受角色、scope、批准标记或工具参数覆盖；这些信息只来自服务端身份、固定工具
注册表和原始持久化提案。审计事件和 API 输出在持久化或导出前脱敏。

M2 创建的历史待审批记录没有可恢复的原始参数，因此迁移后安全标记为不可执行。合法独立
审批人访问这类记录时，服务会终止原调用并要求创建新提案，不会尝试执行脱敏后的参数。

M1 的 `POST /api/v1/refunds` 是仅供已认证 `ADMIN` 业务集成使用的直接服务 API，不在
模型可见的工具注册表内。Agent/LLM 能提出的退款路径只有注册工具 `issue_refund`，该路径
始终进入上述持久化人工审批。

## M4 恢复、取消与副作用控制

Provider 的 429、timeout 和临时 5xx 会进入持久化退避状态；到期后可通过普通 `resume`
继续。不可重试错误和重试耗尽会稳定终止，不在请求线程中 sleep。

注册表内写工具使用唯一 tool call、outbox 租约和 append-only 副作用事件。低风险写工具在
handler 开始前检查取消状态；高风险工具只有在独立审批通过后才创建执行 outbox。

- `POST /api/v1/agent-runs/{run_id}/cancel`：取消流程并终止尚未开始的写调用。
- `POST /api/v1/agent-runs/{run_id}/manual-resume`：提供人工复核原因，可调整上下文和预算，
  并创建新的结构化提案。
- `POST /api/v1/agent-runs/{run_id}/resume`：补充澄清参数，或在持久化退避到期后恢复。

## M5 评测与可观测性

`data/eval/agent_cases.jsonl` 固定 160 条 `m5-v1` 场景，默认只使用确定性 provider 和
隔离的内存数据库。评测同时检查业务状态、首选工具、精确参数、步骤、权限违规、重复副作用
和纯本地编排延迟；失败样本保留完整脱敏 trajectory。

- `GET /api/v1/agent-runs/{run_id}/trajectory`：统一查看 checkpoint、审批、工具、错误、
  副作用和审计证据。
- `GET /metrics`：导出无 PII/租户高基数标签的 Prometheus 指标。
- `POST /api/v1/evaluation/target`：需要 Admin JWT 的 `llm-eval-platform` HTTP target；
  每个场景都在隔离数据库执行，不写生产数据。

OpenTelemetry spans 覆盖 workflow、step、LLM structured call 和 tool execution。架构、
威胁模型及运行手册见 `docs/architecture.md`、`docs/threat-model.md` 和
`docs/runbook.md`。

## M6 MCP、Redis 与企业知识库

生产知识工具通过 `enterprise-rag` 的
`POST /api/v1/knowledge-bases/{knowledge_base_id}/retrieve` 稳定接口检索。服务端根据
已认证 tenant 查询固定的 knowledge-base 映射，并使用服务凭据访问下游；模型不能传入
tenant、角色、token 或 knowledge-base ID。Redis 只承担按租户哈希键的短期缓存和缓存填充
租约，不参与授权，也不是业务事实来源。

`mcp_integration.py` 使用官方 MCP Python SDK 导出固定注册表中的 8 个工具。每个 MCP Server
进程在启动前绑定可信 Principal 和 run ID，写工具幂等键由服务端派生，调用仍然进入现有
`ToolExecutor`、审批、outbox 和审计路径。`scripts/run_mcp_server.py` 提供 stdio transport，
`scripts/smoke_mcp.py` 使用官方 SDK ClientSession 验证端到端通信。

本地和默认测试使用 `KNOWLEDGE_BACKEND=deterministic_stub`。真实模式所需的 bearer token
必须通过环境或 `op://...` 注入；示例变量见 `.env.example`。

## 验证

```bash
mise exec -- uv run ruff check .
mise exec -- uv run pyright
TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  mise exec -- uv run pytest -q
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' \
  mise exec -- uv run python scripts/smoke_workflow.py
mise exec -- uv run python scripts/evaluate_agent.py \
  --dataset data/eval/agent_cases.jsonl
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' KNOWLEDGE_BACKEND=deterministic_stub \
  mise exec -- uv run python scripts/smoke_mcp.py
```
