# Business Workflow Agent 实施计划

`PLAN.md` 只保留范围、里程碑与验收标准。面向读者的项目入口见 [README](README.md)，最近一次
可复现验证及明确限制见 [docs/verification.md](docs/verification.md)，历史架构决策见
[`docs/adr/`](docs/adr/)。

## 目标与不变量

构建企业客服/IT 支持工作流后端。模型负责意图理解、参数补全和操作提案；服务端负责身份、
授权、状态、审批、幂等、副作用、恢复和审计。

- LLM 只能提出操作，不能授权；模型文本、记忆、参数与检索文档均不可信。
- 只执行固定注册、固定 Pydantic schema 的工具；不暴露 SQL 或动态 import。
- 高风险写操作必须进入持久化人工审批，审批人与发起者身份分离。
- 所有外部副作用必须有幂等键；checkpoint replay 不能重复副作用。
- 状态迁移、预算、重试、取消和人工接管由代码显式约束。
- 默认测试不得调用付费 provider；工具日志和导出必须先脱敏。

非目标：通用 BPMN/低代码平台、多 Agent 数量展示、由 prompt 决定权限，以及在没有 live
证据时宣称真实模型或真实企业知识库效果。

## 架构边界

```text
Client/JWT -> FastAPI -> AgentRunner -> StructuredProvider (untrusted proposal)
                         |                |
                         v                v
                    checkpoint       schema repair
                         |
                         v
                    ToolExecutor -> Policy/RBAC -> approval or outbox -> fixed handler
                         |                                      |
                         +---- append-only audit ---------------+
```

技术栈：Python 3.12、FastAPI、Pydantic、PostgreSQL、SQLAlchemy/Alembic、Redis、MCP Python
SDK、OpenTelemetry、Prometheus、Docker Compose、mise/uv。详细信任边界见
[docs/architecture.md](docs/architecture.md)。

## 里程碑状态

### M1：业务后端与固定工具层 — 完成（2026-07-13）

- [x] customer、ticket、refund、approval、workflow run、tool call 与 audit 数据模型。
- [x] CRM/工单/退款模拟 API、JWT、RBAC、scope 和固定工具注册表。
- [x] 精确输入/输出 schema、事务、审计与写操作幂等。

退出条件：事务业务 API、间接/直接越权拒绝、schema 导出校验、同一键重放十次只产生一次
副作用，均已验证。

### M2：Agent 状态机与结构化调用 — 完成（2026-07-13）

- [x] 意图分类、参数提取、澄清、工具提案与结果归纳。
- [x] 显式迁移、事务 checkpoint、持久化事件与服务重启恢复。
- [x] 步数、工具调用、elapsed、token 与 cost 预算。
- [x] 未注册工具拒绝；schema repair 最多一次。

退出条件：受控 schema 合法率 100%、未知工具无法执行、预算稳定终止、checkpoint 可恢复，
均已验证。

### M3：审批与安全边界 — 完成（2026-07-15）

- [x] 执行器内基于服务端 Principal 授权，忽略模型伪造角色/批准字段。
- [x] 高风险工具持久化暂停；独立审批人和身份绑定的一次性 token。
- [x] approve/reject/expired/already-used、自批、注入、参数篡改与跨租户测试。
- [x] PII 脱敏与 append-only 审计。

退出条件：高风险审批覆盖 100%、未授权成功数 0、攻击集绕过数 0、拒绝后不可恢复原调用，
均已验证。

### M4：恢复、取消与副作用控制 — 完成（2026-07-16）

- [x] retryable/non-retryable 分类；429、timeout、5xx 有界指数退避与 jitter。
- [x] 写工具唯一调用、outbox 租约、append-only 副作用事件和 replay 防重。
- [x] cancel、manual review/resume 与全部 17 个持久化状态故障注入。

退出条件：每个状态可恢复或确定失败、十次 replay 无重复工单/退款、不可重试错误不循环、
取消后不启动待处理写工具，均已验证。

### M5：评测、可观测性与交付 — 完成（2026-07-16）

- [x] 160 条 `m5-v1` 场景，覆盖正常、歧义、攻击、超时、审批、拒绝和 replay。
- [x] 业务状态、首选工具、参数、权限、重复副作用和编排耗时的确定性 release gate。
- [x] 脱敏 trajectory、Prometheus、OpenTelemetry 与 `llm-eval-platform` HTTP target。
- [x] 架构、威胁模型、运行手册和 CLI 演示。

退出条件：固定场景成功率和首选工具准确率均 >=90%、编排 p95 <=200ms、单 run 轨迹完整，
均已在**确定性 provider**矩阵验证。该结果不证明 live LLM 质量。

### M6：Redis、MCP 与 enterprise-rag 边界 — 完成（2026-07-16）

- [x] 真实 Redis 的 tenant 隔离哈希缓存和 token-owned 填充租约；失败时只读 fail-open。
- [x] enterprise-rag 固定 HTTP schema、服务端 bearer/tenant/knowledge-base 注入与失败分类。
- [x] 官方 MCP SDK 导出 8 个固定工具，可信 Principal/run 绑定并复用 ToolExecutor。
- [x] PostgreSQL、Redis、MCP、RAG stub、provider stub 集成矩阵与 smoke。

退出条件：Redis 隔离/租约、RAG 协议和失败矩阵、MCP schema/权限/重放、完整无 skip 验收均
已验证。真实 enterprise-rag live round-trip 未执行，因此保持 `unverified`。

### M7：Live evidence 与 claim 对齐 — 实施中（2026-07-23）

- [x] REST 与 Agent/tool 的高风险退款统一进入持久化独立审批，并记录来源。
- [x] OpenAI-compatible HTTP transport、显式运行时配置与失败分类；禁止静默回退。
- [x] 84 条中英平衡 opt-in live 数据集、真实 provider evaluator 与 RAG/Redis smoke。
- [x] CI branch coverage 92% 门禁、Gitleaks 与可下载 deterministic evidence。
- [x] 完成 84 条 Ollama live run 并保存脱敏报告；安全门禁通过，模型任务成功率仅
  2/84，按原值保留。
- [ ] 完成真实 enterprise-rag HTTP/Redis/tenant round-trip。

退出条件：默认 deterministic gate 保持通过；live evaluator 完整运行 84 条且权限违规和重复
副作用均为 0；真实 RAG smoke 验证命中、cache hit、Redis outage fail-open 与错误租户拒绝；
简历、README 和静态展示只采用本轮真实复测证据。未完成的 live 项不得由 stub 结果替代。

## 总体验收标准

| 类别 | Release gate | 状态 |
|---|---|---|
| Schema | 受控工具参数合法率 100% | verified |
| 确定性场景 | >=150 条，端到端成功率 >=90% | verified：160/160 |
| 工具选择 | 首选工具准确率 >=90% | verified：100% |
| 权限与审批 | 越权成功 0；高风险 100% 审批 | verified |
| 幂等 | 同一请求十次 replay 仅一次副作用 | verified |
| 恢复与取消 | 任意持久化状态恢复/确定失败；取消阻止待执行写入 | verified |
| 注入安全 | 固定攻击集权限绕过 0 | verified |
| 审计 | user/tenant/run/state/tool/脱敏参数/结果/时间可关联 | verified |
| 性能 | 纯编排 p95 <=200ms | verified（确定性本地矩阵） |
| Live LLM | 真实模型质量与成本 | verified：本地 Ollama 84/84 已测，质量很差，非默认 release gate |
| Live RAG | 真实 enterprise-rag 检索质量 | unverified，非默认 release gate |
| 高风险 REST | 与 Agent/tool 相同的独立审批与 replay 语义 | verified |
| Live evidence | Ollama 84 条与 enterprise-rag round-trip | partial：Ollama verified；RAG unverified |

## 验收命令

```bash
mise exec -- uv sync --frozen
mise exec -- uv run ruff check .
mise exec -- uv run pyright
docker compose up -d --build
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
```

测试矩阵必须覆盖 unit、migration、integration、security、fault injection、evaluation 和 E2E；
收集结果不得是 0 tests，必需 PostgreSQL/Redis 门控不得 skip。最近一次实际命令、数量、覆盖率、
非发现项与未验证项记录在 [docs/verification.md](docs/verification.md)。

## 风险与工具记录

- 模型不稳定：结构化提案、有限注册表和确定性 policy 收口；仍需单独 live eval。
- 重复副作用：唯一约束、幂等键、outbox 和 replay tests；外部系统仍需接受幂等键。
- 审批伪造：审批身份与 Agent 分离，服务端校验一次性 token。
- 日志泄密：持久化/导出前脱敏；发布前运行 gitleaks。
- 框架锁定：核心 domain/policy/executor 不依赖编排框架类型。

未安装全局工具。项目通过 `.mise.toml` 固定 Python 3.12.13 与 uv 0.11.28；首次安装命令
`mise install`，卸载命令 `mise uninstall uv@0.11.28`。
