# 工单客服与业务流程 Agent 实施计划

## 1. 项目定位

构建一个面向企业客服/IT 支持场景的业务流程 Agent。模型负责意图理解、参数补全和行动建议；后端负责权限、状态、审批、幂等、工具执行和审计。

典型流程包括查询客户、检索知识库、创建或更新工单、计算退款、发起高风险审批、执行失败重试以及人工接管。

项目重点不是“多个 Agent 聊天”，而是证明 LLM 能够安全接入有副作用的业务系统。

### 目标岗位信号

- Function Calling 与严格 JSON Schema
- 显式状态机和持久化工作流
- PostgreSQL 业务建模与事务
- MCP/业务 API 集成
- RBAC、工具权限和人工审批
- 幂等执行、失败恢复和审计日志
- Agent trajectory 评测与可观测性

### 非目标

- 不允许 LLM 直接执行 SQL 或绕过服务层授权。
- 不允许 Prompt 决定最终权限。
- 不构建通用 BPMN/低代码平台。
- 不把多 Agent 数量作为项目亮点。

## 2. GitHub 调研基线

- [LangGraph](https://github.com/langchain-ai/langgraph)：参考 durable execution、checkpoint、Human-in-the-loop 和长时状态工作流。
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python)：参考 tools、guardrails、handoffs、sessions 和 tracing 抽象。
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)：参考标准 MCP Server/Client 和工具 schema。
- [Temporal Python SDK](https://github.com/temporalio/sdk-python)：只参考可靠工作流和重放语义；MVP 不一定引入 Temporal。

## 3. 推荐技术栈

- API：Python、FastAPI、Pydantic
- 编排：自研显式状态机（见 `docs/adr/0001-explicit-state-machine.md`；M2 实现）
- 数据库：PostgreSQL、SQLAlchemy、Alembic
- 缓存/锁：Redis
- 工具协议：Pydantic JSON Schema + MCP Python SDK
- 知识工具：调用 `enterprise-rag` 的稳定 API
- 模型：OpenAI-compatible Provider 接口
- 观测：OpenTelemetry、Prometheus、结构化审计日志
- 部署：Docker Compose

## 4. 业务边界与状态机

```text
RECEIVED
  -> CLASSIFY
  -> CLARIFY (missing fields)
  -> RETRIEVE (knowledge-only request)
  -> PLAN_ACTION
  -> VALIDATE_POLICY
       -> EXECUTE (low risk)
       -> AWAIT_APPROVAL (high risk)
  -> VERIFY_RESULT
  -> COMPLETE

Any step
  -> RETRYABLE_FAILURE -> RETRY
  -> NON_RETRYABLE_FAILURE -> MANUAL_REVIEW
  -> CANCELLED
```

### 初始业务工具

- `search_knowledge_base`
- `get_customer`
- `list_customer_tickets`
- `create_ticket`
- `update_ticket`
- `calculate_refund`
- `issue_refund`
- `request_human_approval`

### 工具风险等级

- `READ_ONLY`：查询知识库、客户、历史工单。
- `WRITE_LOW_RISK`：创建工单、添加备注。
- `WRITE_HIGH_RISK`：退款、关闭重要工单、修改客户关键字段。

所有工具必须拥有 JSON Schema、timeout、重试策略、权限 scope、幂等键、审计事件和脱敏规则。

## 5. 里程碑

### M1：业务后端与工具层

**状态：已完成（2026-07-13）。** 范围止于业务后端与工具层；未实现 M2 状态迁移、
checkpoint、LLM 调用或预算执行循环。

#### 工作内容

- [x] 建立 customer、ticket、ticket_event、approval、workflow_run、tool_call、audit_event
  数据模型，并增加实际退款副作用所需的 refund 模型。
- [x] 完成模拟 CRM、工单、退款服务的 REST API。
- [x] 完成 JWT、RBAC 和 scope 校验。
- [x] 建立固定工具注册表和工具元数据。
- [x] 为每个工具定义输入/输出 Pydantic 模型。
- [x] 在无 LLM 情况下验证全部 M1 业务操作。

#### 退出条件

- [x] 工单创建、更新和退款模拟 API 具备事务测试。
- [x] 未授权用户无法直接或间接调用写工具。
- [x] 工具 schema 可导出并通过 JSON Schema 校验。
- [x] 同一幂等键重复调用写工具 10 次只产生一次副作用。

#### M1 实际验证记录

| 命令 | 结果 |
|---|---|
| `mise exec -- uv sync` | 通过；解析 44 个包，检查 41 个已安装包 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra` | 通过；22 passed，无 skip、无 0 tests |
| `docker compose up -d --build` | 通过；PostgreSQL 17 容器 healthy |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic upgrade head` | 通过；升级至 `20260713_0001` |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic check` | 通过；No new upgrade operations detected |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/integration` | 通过；7 passed |
| `mise exec -- uv run pytest -q -ra tests/security` | 通过；6 passed |
| `mise exec -- uv run pytest -q -ra tests/fault_injection` | 通过；1 passed |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow JWT_SECRET=<local-test-secret> mise exec -- uv run python scripts/smoke_workflow.py` | 通过；10 次建单重放产生 1 次副作用，高风险工具进入审批，退款模拟 API 成功 |
| `gitleaks detect --source . --no-git --redact --exit-code 1` | 通过；no leaks found |

### M2：Agent 状态机与结构化调用

**状态：已完成（2026-07-13）。** 范围止于结构化 Provider、显式状态迁移、事务型
checkpoint、预算终止和持久化 SSE 事件；未实现 M3 的审批决策 token，也未实现 M4 的
provider 重试、退避、outbox 或取消语义。

#### 工作内容

- [x] 实现意图分类、参数提取、澄清、工具选择、结果归纳。
- [x] 每一步状态写入数据库 checkpoint。
- [x] 设置最大步数、最大工具调用数、elapsed time、token 和费用预算。
- [x] 禁止模型生成工具名以外的动态执行路径。
- [x] 将 schema validation error 返回给修复节点，最多重试一次。
- [x] 支持流式事件：thought summary、tool proposed、approval required、complete。

#### 退出条件

- [x] 受控测试中工具调用参数 schema 合法率为 100%。
- [x] Agent 无法调用未注册工具。
- [x] 超过预算或最大步数后稳定终止，不出现无限循环。
- [x] 服务重启后可从最后一个已提交 checkpoint 恢复。

#### M2 实际验证记录

| 命令 | 结果 |
|---|---|
| M2 目标测试（实现前） | 预期失败；5 个 collection error，均为尚不存在的 workflow/provider/checkpoint 接口 |
| `mise exec -- uv sync` | 通过；解析 44 个包，检查 41 个已安装包 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 50 tests，无 0 tests |
| `mise exec -- uv run pytest -q -ra` | 通过；49 passed、1 skipped；唯一 skip 是未设置 `TEST_DATABASE_URL` 的 PostgreSQL migration 环境门 |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra` | 通过；50 passed，无 skip |
| `docker compose up -d --build && docker compose ps` | 通过；PostgreSQL 17 容器 healthy |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic upgrade head` | 通过；从 `20260713_0001` 升级至 `20260713_0002` |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic check` | 通过；No new upgrade operations detected |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/integration` | 通过；13 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/security` | 通过；9 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/fault_injection` | 通过；3 passed |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow JWT_SECRET=<local-test-secret> mise exec -- uv run python scripts/smoke_workflow.py` | 通过；知识流程到 `COMPLETE`，退款流程到 `AWAIT_APPROVAL`，3 类完成事件持久化，checkpoint 数与 run version 一致；销毁原 engine 后由新应用从 PostgreSQL checkpoint 恢复到 `COMPLETE` |
| `gitleaks detect --source . --no-git --redact --exit-code 1` | 通过；no leaks found |

### M3：权限、审批与安全边界

**状态：已完成（2026-07-15）。** 范围止于 Agent/LLM 可提议工具的执行器授权、
持久化人工审批、独立审批身份、一次性 decision token、拒绝/过期终态、PII 脱敏和
append-only 审计，并安全阻止缺少原始参数的 M2 历史审批执行；未实现 M4 的 provider
重试、指数退避、outbox、取消或全状态故障注入。

这里的“高风险操作”指固定工具注册表内、Agent/LLM 可提议的高风险工具。M1 的管理员
直接业务 API 不在模型工具注册表内，并继续由服务端 JWT、角色和 scope 单独保护。

#### 工作内容

- [x] 将授权决策放在工具执行器，不信任 LLM 输出的角色或批准信息。
- [x] 高风险工具自动创建 approval record 并暂停 workflow。
- [x] 批准/拒绝使用独立用户身份和一次性 decision token。
- [x] 增加 prompt injection、tool injection 和参数篡改测试。
- [x] 对工具参数和输出做 PII 脱敏。
- [x] 审计日志采用 append-only 语义。

#### 退出条件

- [x] 高风险操作 100% 进入人工审批。
- [x] 未授权高风险操作成功数为 0。
- [x] 预设 prompt-injection 集合中权限绕过成功数为 0。
- [x] 审批拒绝后原工具不能被恢复流程再次执行。

#### M3 实际验证记录

| 命令 | 结果 |
|---|---|
| M3 目标测试（实现前） | 预期失败；7 failed、21 passed、1 skipped，失败均对应尚不存在的审批端点、迁移字段和状态迁移 |
| `mise exec -- uv sync` | 通过；解析 44 个包，检查 41 个已安装包 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings |
| M3 历史审批兼容测试（实现前） | 预期失败；2 failed，分别证明旧审批仍会签发 token、迁移缺少安全可执行标志 |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest --collect-only -q` | 通过；收集 66 tests，无 0 tests |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra` | 通过；66 passed，无 skipped、xfail 或环境 gating |
| `docker compose up -d --build && docker compose ps` | 通过；PostgreSQL 17 容器 healthy |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic upgrade head` | 通过；从 `20260713_0002` 经审批字段迁移升级至 `20260715_0004`，历史审批默认不可执行 |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic current` | 通过；`20260715_0004 (head)` |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic check` | 通过；No new upgrade operations detected |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/integration` | 通过；18 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/security` | 通过；18 passed；6/6 预设 prompt injection 均未绕过权限 |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/fault_injection` | 通过；3 passed |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow JWT_SECRET=<local-test-secret> mise exec -- uv run python scripts/smoke_workflow.py` | 通过；批准流程到 `COMPLETE` 且退款恰好 1 条，拒绝流程到 `NON_RETRYABLE_FAILURE` 且无退款，自批被拒、decision token 重放冲突、checkpoint 数与当前 version 一致 |
| `gitleaks detect --source . --no-git --redact --exit-code 1` | 通过；no leaks found |

### M4：失败恢复与副作用控制

**状态：已完成（2026-07-16）。** 范围止于持久化 provider 重试、全状态 checkpoint
恢复、注册写工具 outbox、append-only 副作用轨迹、取消和人工恢复；M5 的场景集、评测、
指标和 trace 不计入本里程碑范围。

这里的 outbox 覆盖固定工具注册表内、Agent/LLM 可提议的写工具。M1 的管理员直接业务 API
不是模型工具，并继续使用同步数据库事务、服务端授权和唯一 tool call 幂等记录。

#### 工作内容

- [x] 区分 retryable 与 non-retryable error。
- [x] 对 429、timeout 和临时 5xx 实现指数退避和 jitter。
- [x] 写工具使用 outbox/idempotency record，避免 checkpoint 重放导致重复副作用。
- [x] 实现 workflow cancel、manual review 和 resume。
- [x] 对每个状态点建立进程终止故障注入。

#### 退出条件

- [x] 在每个状态点终止 API/Worker 后均可恢复或进入明确失败状态。
- [x] 同一 workflow 重放 10 次不重复创建工单或退款。
- [x] 不可重试错误不会进入无限重试。
- [x] 被取消的 workflow 不会继续执行待处理写工具。

#### M4 实际验证记录

| 命令 | 结果 |
|---|---|
| M4 目标测试（实现前） | 预期失败；4 个 collection error，分别对应缺失的重试策略、provider 错误分类和 outbox 模型 |
| `mise exec -- uv sync` | 通过；解析 44 个包，检查 41 个已安装包 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest --collect-only -q` | 通过；收集 81 tests，无 0 tests |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra` | 通过；81 passed，无 skipped 或 xfail；唯一条件 skip 声明已由显式 PostgreSQL URL 解门 |
| `docker compose up -d --build` 与 `docker compose ps` | 通过；PostgreSQL 17 容器 healthy |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic upgrade head` | 通过；从 `20260715_0004` 升级至 `20260716_0006`，包含恢复/outbox 与 append-only 副作用事件迁移 |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic current` | 通过；`20260716_0006 (head)` |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic check` | 通过；No new upgrade operations detected |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/integration` | 通过；22 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/security` | 通过；18 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/fault_injection` | 通过；8 passed；实际遍历全部 17 个 workflow 状态，并覆盖领取后崩溃、租约恢复、provider 重启恢复和取消竞争边界 |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow JWT_SECRET=<local-test-secret> mise exec -- uv run python scripts/smoke_workflow.py` | 通过；工单重放 10 次和退款决定重放 10 次均只有 1 次副作用，写 outbox 成功，取消后的待处理写副作用为 0，PostgreSQL 重启恢复到 `COMPLETE` |
| `gitleaks detect --source . --no-git --redact --exit-code 1` | 通过；no leaks found |

#### M4 本轮先行审计（2026-07-16）

| 审计项 | 结论 | 复验证据 |
|---|---|---|
| 每个持久化状态终止后恢复或确定失败 | verified | M5 实现前重新运行 fault suite：8 passed；测试实际遍历全部 17 个 workflow 状态 |
| workflow 重放 10 次不重复工单或退款 | verified | M5 实现前 smoke 再次确认 `ticket_replays=10`、`refund_decision_replays=10`，每类仅 1 次副作用 |
| 不可重试错误不无限重试 | verified | provider fault regression 通过，non-retryable 和 retry-exhausted 均稳定终止 |
| 取消后不开始待处理写工具 | verified | 取消前边界及 outbox claim 后竞争边界测试通过，smoke 确认副作用为 0 |
| skipped、0 tests、环境 gating | non-finding | 收集 81 tests；无 skipped/xfail；唯一 `TEST_DATABASE_URL` 条件门控由显式 PostgreSQL URL 解开 |
| 未覆盖路径是否构成退出条件缺口 | non-finding | 分支覆盖率 90%；未覆盖行未暴露新的 M4 退出条件缺口 |

本轮 smoke 首次遗漏必需的测试 `JWT_SECRET`，在按上表已记录的完整命令补齐环境变量后通过；
这是调用环境错误，不是产品实现失败。M4 未发现 `failed` 或必需的 `unverified` 退出条件，
因此才开始 M5。

### M5：Agent 评测、观测与交付

**状态：已完成（2026-07-16）。** 范围止于版本化确定性场景评测、独立
`llm-eval-platform` HTTP 接入、统一脱敏 trajectory、OpenTelemetry/Prometheus 观测和
交付文档；未引入真实付费模型、远程 telemetry backend 或生产部署。

#### 工作内容

- [x] 建立至少 150 条场景数据集。
- [x] 场景覆盖知识问答、参数缺失、多工具、越权、注入、超时、审批和重复请求。
- [x] 评测任务成功、工具选择、参数正确、步骤数、权限违规和重复副作用。
- [x] 接入 `llm-eval-platform`。
- [x] 建立 workflow/tool/LLM trace 和 Prometheus 指标。
- [x] 编写架构、威胁模型、运行手册和演示脚本。

#### 退出条件

- [x] 150 条场景端到端成功率不低于 90%。
- [x] 首选工具准确率不低于 90%。
- [x] 不含 LLM 和工具本身耗时的编排开销 p95 不高于 200ms。
- [x] 单次 run 可完整查看状态迁移、审批、工具调用和错误。

#### M5 实际验证记录

| 命令 | 结果 |
|---|---|
| M5 目标测试（实现前） | 预期失败；3 个 collection error，分别对应缺失的 evaluation 模块、observability 模块和 OpenTelemetry 项目依赖 |
| `mise exec -- uv add 'opentelemetry-api>=1.38,<2' 'opentelemetry-sdk>=1.38,<2' 'prometheus-client>=0.23,<1'` | 通过；解析 48 个包，项目本地安装 OpenTelemetry 1.43.0 与 prometheus-client 0.25.0；无全局安装 |
| `mise exec -- uv sync --frozen` | 通过；检查 45 个已安装包，lockfile 无漂移 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest --collect-only -q` | 通过；收集 89 tests，无 0 tests；无 skipped/xfail，唯一 PostgreSQL 条件门控已解开 |
| `mise exec -- uv run pytest -q -ra tests/unit/test_m5_evaluation_dataset.py tests/integration/test_m5_evaluation.py tests/integration/test_m5_observability.py` | 通过；8 passed，覆盖数据集边界、失败 gate、8 类真实 E2E、平台 target、跨租户/脱敏 trajectory、单 run 审批/工具/错误、metrics 与 spans |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra` | 通过；89 passed，无 skipped 或 xfail |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/integration` | 通过；27 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/security` | 通过；18 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra tests/fault_injection` | 通过；8 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra --cov=business_workflow_agent --cov-branch --cov-report=term-missing` | 通过；89 passed；总分支覆盖率 91%，M5 evaluation 92%、observability 98%、trajectory 96% |
| `mise exec -- uv run python scripts/evaluate_agent.py --dataset data/eval/agent_cases.jsonl` | 通过；160/160 case success，首选工具与参数准确率均 100%，权限违规 0、重复副作用 0，保守编排 p95 20.752ms，release gate 通过 |
| 使用本机 `llm-eval-platform` 的 `parse_import` 与 `TargetSpec.model_validate` 验证当前 dataset/config | 通过；导入 160 cases、8 个 task type，HTTP target schema 合法 |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow JWT_SECRET=<local-test-secret> mise exec -- uv run python scripts/smoke_workflow.py` | 通过；M1-M4 工单/退款/审批/取消/重启恢复 smoke 保持成功 |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow JWT_SECRET=<local-test-secret> mise exec -- uv run python scripts/demo_workflow.py` | 通过；单次演示可见审批，越权 run 暴露 `POLICY_DENY_ROLE`，Prometheus 指标成功导出 |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic current` 与 `alembic check` | 通过；`20260716_0006 (head)`，No new upgrade operations detected；M5 无 schema 变更 |
| `gitleaks detect --source . --no-git --redact --exit-code 1` | 通过；no leaks found |

延迟采用包含确定性本地 provider stub 和本地工具耗时的保守上界；该上界已远低于 200ms，
因此严格排除 LLM/工具耗时后的编排值同样满足退出条件。完整失败 trajectory 会写入可选 report；
本次 160 条无失败样本。

#### M5 本轮退出条件复核（2026-07-16，再次复验）

本轮再次先只读复核了源码、测试、场景数据、Git 状态和既有验证记录。实现保护存在，但初始测试
未直接锁定数据集空文件/坏 JSON/重复 ID/路由字段不一致、评测 target 的非 Admin 拒绝，以及
trajectory 的同租户所有权和 Auditor 读取边界；已在 M5 测试内补齐这些回归断言，没有扩展功能
范围。`PLAN.md` 未定义 M5 之后的新里程碑，因此本轮未创建或实现 M6。

| 命令 | 结果 |
|---|---|
| `docker compose up -d --build` 与 `docker compose ps` | 通过；PostgreSQL 17 容器 healthy |
| `mise exec -- uv sync --frozen` 与 `mise exec -- uv lock --check` | 通过；检查 45 个已安装包，解析 48 个包，lockfile 无漂移 |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest --collect-only -q` | 通过；收集 90 tests，无 0 tests；唯一显式 `pytest.skip` 是 PostgreSQL URL 门控，本命令和下列全套测试均已解门 |
| `mise exec -- uv run pytest -q -ra tests/unit/test_m5_evaluation_dataset.py tests/integration/test_m5_evaluation.py tests/integration/test_m5_observability.py` | 通过；9 passed；新增覆盖数据集损坏/重复/路由不一致、非 Admin target 拒绝、同租户非所有者拒绝和 Auditor 读取 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings、0 informations |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra` | 通过；90 passed，无 skipped 或 xfail |
| 同一 PostgreSQL 环境分别运行 `pytest -q -ra tests/integration`、`tests/security`、`tests/fault_injection` | 通过；分别 27、18、8 passed |
| `TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run pytest -q -ra --cov=business_workflow_agent --cov-branch --cov-report=term-missing` | 通过；90 passed；总分支覆盖率 91%，M5 evaluation 95%、observability 98%、trajectory 100% |
| `mise exec -- uv run python scripts/evaluate_agent.py --dataset data/eval/agent_cases.jsonl --report /tmp/business-workflow-agent-m5-reaudit-repeat.json` | 通过；160/160 case success，首选工具和参数准确率均 100%，权限违规 0、重复副作用 0，保守编排 p95 22.887ms，release gate 通过，160 条 trajectory 均非空 |
| 使用本机 `llm-eval-platform` 的 `parse_import` 与 `TargetSpec.model_validate_json` 复核 dataset/config | 通过；导入 160 cases、8 个 task type，HTTP target schema 合法 |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow mise exec -- uv run alembic upgrade head`、`current`、`check` | 通过；`20260716_0006 (head)`，No new upgrade operations detected |
| `DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow JWT_SECRET=<local-test-secret> mise exec -- uv run python scripts/smoke_workflow.py` | 通过；10 次工单重放只产生 1 次副作用，10 次退款决定重放只产生 1 笔退款，审批拒绝/批准、取消和 PostgreSQL 重启恢复均成功 |
| 同一数据库环境运行 `scripts/demo_workflow.py` | 通过；审批可见，越权错误 `POLICY_DENY_ROLE` 可见，Prometheus 指标成功导出 |
| `gitleaks detect --source . --no-git --redact --exit-code 1` | 通过；no leaks found |

逐项结论：150 条场景成功率、首选工具准确率、编排 p95 和单 run 完整 trajectory 均为
`verified`；skipped/xfail、0 tests、未解开的环境门控和构成退出条件缺口的剩余未覆盖路径均为
`non-finding`。真实付费模型、远程 telemetry backend 和生产部署仍是明确的非 M5 范围，未据此
宣称已验证。

### M6：Redis、MCP 与企业知识库集成

**状态：已完成（2026-07-16）。** 范围止于 Redis 缓存/租约、官方 MCP Python SDK
Server/Client 适配、`enterprise-rag` 稳定检索 API 客户端和对应确定性集成测试；默认测试不
连接远程知识库，真实 `enterprise-rag` 部署仍使用独立服务凭据和显式 tenant 映射。

#### 工作内容

- [x] 使用真实 Redis 容器实现按 tenant 隔离、隐藏原始查询的知识响应缓存和 token-owned
  缓存填充租约；Redis 故障只对读路径 fail-open，不成为授权或事实来源。
- [x] 按 `enterprise-rag` 的稳定 retrieval schema 实现 HTTP 客户端，服务端注入 bearer
  token、tenant 和固定 knowledge-base 映射，区分授权、未找到、畸形响应和重试耗尽。
- [x] 使用 MCP Python SDK 导出固定 Pydantic schema；Server 绑定可信 Principal 和 run ID，
  调用仍进入 `ToolExecutor`，写工具幂等键由服务端派生。
- [x] 提供可运行的 stdio MCP Server 与官方 SDK Client smoke。
- [x] 增加 ADR、架构、威胁模型、运行手册和环境变量文档。

#### 退出条件

- [x] 真实 Redis 缓存命中、跨 tenant 隔离、原始查询不出现在 key 中，且同一租约不能并发
  获取。
- [x] RAG stub 验证下游 URL、bearer、tenant header 和精确 request/response schema；429、
  timeout、5xx 有界重试，401/403/404、畸形响应、缺失 tenant 映射和重试耗尽确定失败。
- [x] MCP Client 能发现全部 8 个固定工具及精确 schema；身份注入字段被拒绝，缺少 scope
  无法执行，写工具重复调用只产生一次副作用。
- [x] PostgreSQL、Redis、MCP、RAG stub 和 Provider stub 集成矩阵均由真实测试覆盖。
- [x] 完整测试、lint、类型检查、迁移、workflow/MCP smoke、评测和泄密扫描全部通过，且
  无 skipped/xfail、0 tests 或未解开的环境门控。

#### M6 实际验证记录

| 命令 | 结果 |
|---|---|
| M6 目标测试（实现前） | 预期失败；3 个 collection error，分别对应缺失的 `knowledge` 与 `mcp_integration` 模块 |
| `mise exec -- uv add 'httpx>=0.28,<1' 'redis>=6,<8' 'mcp>=1.26,<2'` | 通过；解析 60 个包，项目本地安装 httpx 0.28.1、redis 7.4.1、mcp 1.28.1；无全局安装 |
| `docker compose up -d --build` 与 `docker compose ps` | 通过；PostgreSQL 17 与 Redis 7.4 容器均 healthy |
| `mise exec -- uv sync --frozen` 与 `mise exec -- uv lock --check` | 通过；检查 56 个已安装包，解析 60 个包，lockfile 无漂移 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings、0 informations |
| `TEST_DATABASE_URL=... TEST_REDIS_URL=... mise exec -- uv run pytest --collect-only -q -ra` | 通过；收集 107 tests，无 0 tests；PostgreSQL 与 Redis 环境门控均已解开 |
| M6 聚焦测试 | 通过；17 passed，覆盖 HTTP 正常/边界/失败、真实 Redis、MCP SDK Client/Server、身份注入拒绝与写操作重放 |
| `TEST_DATABASE_URL=... TEST_REDIS_URL=... DATABASE_URL=... mise exec -- uv run pytest -q -ra` | 通过；107 passed，无 skipped 或 xfail |
| 同一 PostgreSQL/Redis 环境分别运行 `pytest -q -ra tests/integration`、`tests/security`、`tests/fault_injection` | 通过；分别 29、19、8 passed |
| 同一环境运行 `pytest -q -ra --cov=business_workflow_agent --cov-branch --cov-report=term-missing` | 通过；107 passed；总分支覆盖率 92%，M6 knowledge 94%、MCP integration 96% |
| `DATABASE_URL=... mise exec -- uv run alembic upgrade head`、`current`、`check` | 通过；`20260716_0006 (head)`，No new upgrade operations detected；M6 无 schema 变更 |
| 首轮在同一 shell 导出 `DATABASE_URL` 后运行全套测试 | 发现 2 个 SQLite migration 测试被环境 URL 覆盖；修正 Alembic 显式测试 URL 优先级后，目标 migration 测试 3 passed，全套重新验证 107 passed |
| `DATABASE_URL=... JWT_SECRET=... KNOWLEDGE_BACKEND=deterministic_stub mise exec -- uv run python scripts/smoke_workflow.py` | 通过；既有工单/退款/审批/取消/恢复与十次重放行为保持成功 |
| 同一环境运行 `scripts/demo_workflow.py` | 通过；审批、越权错误与 Prometheus 指标可见 |
| 同一环境运行 `scripts/smoke_mcp.py` | 通过；官方 SDK stdio Client/Server 发现 8 个工具，知识调用 `SUCCEEDED` 并返回 1 条结果 |
| `mise exec -- uv run python scripts/evaluate_agent.py --dataset data/eval/agent_cases.jsonl --report /tmp/business-workflow-agent-m6-audit.json` | 通过；160/160，工具和参数准确率 100%，权限违规与重复副作用均为 0，编排 p95 21.621ms |
| `gitleaks detect --source . --no-git --redact --exit-code 1` | 通过；no leaks found |

逐项结论：Redis、MCP、RAG stub、Provider stub 与 PostgreSQL 集成矩阵均为 `verified`；
skipped/xfail、0 tests、未解开的门控、原始查询出现在 Redis key、MCP 身份注入和重复副作用均为
`non-finding`。未使用真实业务 token 启动独立 `enterprise-rag` 部署，因此该 live round-trip 为
`unverified`；默认验收使用 PLAN 明确要求的 RAG stub，不据此宣称生产凭据或外部数据已验证。

## 6. 总体验收标准

| 类别 | 验收标准 |
|---|---|
| Schema | 受控测试中工具调用参数合法率 100% |
| 成功率 | 150 条固定场景端到端成功率 >= 90% |
| 工具选择 | 首选工具准确率 >= 90% |
| 权限 | 未授权高风险操作成功数为 0 |
| 审批 | 高风险操作 100% 进入审批节点 |
| 幂等 | 同一请求重放 10 次只产生一次副作用 |
| 恢复 | 任意状态点终止后可恢复或确定失败 |
| 注入安全 | 固定攻击集中权限绕过成功数为 0 |
| 审计 | 每次调用可关联 user、run、tool、参数摘要和结果 |
| 性能 | 纯编排开销 p95 <= 200ms |

## 7. 计划验收命令

```bash
mise exec -- uv sync
mise exec -- uv run ruff check .
mise exec -- uv run pyright
docker compose up -d --build
TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 mise exec -- uv run pytest -q
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
  mise exec -- uv run alembic upgrade head
TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  mise exec -- uv run pytest -q tests/integration
TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  mise exec -- uv run pytest -q tests/security
TEST_DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  mise exec -- uv run pytest -q tests/fault_injection
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' KNOWLEDGE_BACKEND=deterministic_stub \
  mise exec -- uv run python scripts/smoke_workflow.py
DATABASE_URL=postgresql+psycopg://workflow@127.0.0.1:54329/workflow \
JWT_SECRET='<local-random-value>' KNOWLEDGE_BACKEND=deterministic_stub \
  mise exec -- uv run python scripts/smoke_mcp.py
mise exec -- uv run python scripts/evaluate_agent.py --dataset data/eval/agent_cases.jsonl
```

## 8. 测试矩阵

| 层级 | 必测内容 |
|---|---|
| 单元 | schema、policy、state transition、retry、redaction |
| 数据库 | migration、outbox、幂等唯一约束、审计 append-only |
| 集成 | PostgreSQL、Redis、MCP、RAG stub、Provider stub |
| 权限 | role/scope、跨租户、伪造 approval、直接工具调用 |
| 安全 | prompt injection、tool injection、PII 泄漏、恶意参数 |
| 故障 | timeout、429、5xx、进程终止、重复消息、恢复 |
| 评测 | 成功率、工具选择、参数、步骤数、副作用 |
| E2E | 咨询、建单、补充参数、退款审批、拒绝、人工接管 |

## 9. 主要风险

- **LLM 决策不稳定**：用显式状态机、有限工具集合和确定性 policy 收口。
- **重复副作用**：所有写工具强制幂等键并配合 outbox/执行记录。
- **审批被伪造**：审批身份与 Agent 会话分离，不接受模型文本作为批准。
- **评测只测 happy path**：至少一半数据覆盖异常、安全和恢复路径。
- **框架锁定**：核心 domain、policy 和 tool executor 不依赖 LangGraph 类型。
- **日志泄密**：默认脱敏工具参数、模型输入和业务输出。

## 10. 工具安装记录

本项目未安装全局软件；运行时和项目工具由 `mise`/`uv` 固定。

| 时间 | 工具 | 安装命令 | 原因 | 卸载命令 |
|---|---|---|---|---|
| 2026-07-13 | uv 0.11.28（mise 项目工具） | `mise install` | 按 `.mise.toml` 固定依赖管理器并创建可复现环境 | `mise uninstall uv@0.11.28` |
