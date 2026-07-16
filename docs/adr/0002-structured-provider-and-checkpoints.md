# ADR 0002：结构化 Provider 边界与事务型 checkpoint

- 状态：Accepted
- 日期：2026-07-13
- 决策范围：M2 Agent 状态机与结构化调用

## 背景

M2 需要让模型完成意图分类、参数提取、一次 schema 修复和结果归纳，同时保持
工具选择、授权、预算和持久化状态由服务端控制。流程还必须在进程重启后从最后一个
已提交状态继续，而不能依赖进程内会话。

## 决策

核心层使用 `StructuredProvider` 协议，只接受经过 Pydantic 校验的提案和结果摘要。
`OpenAICompatibleProvider` 只负责把严格 JSON Schema 交给注入的传输适配器；默认测试
使用明确标记、无远程调用的 `DeterministicProvider`。

模型返回的工具名不作为动态分派依据。执行前必须同时满足：intent 对应服务端固定工具名、
工具已注册、参数 schema 合法、服务端身份和 scope 授权通过。

每次合法状态迁移都在同一数据库事务中更新 `workflow_run` 的版本和预算计数，并追加唯一
`(run_id, version)` 的 `workflow_checkpoint`。面向客户端的流式事件先脱敏，再以唯一
`(run_id, sequence)` 持久化；SSE 端点只读取这些持久化事件。

## 影响

- 业务状态和恢复语义不依赖具体模型 SDK 或编排框架。
- 服务重启只需重新创建 runner，即可从数据库中的当前状态继续。
- schema 修复最多执行一次，第二次失败进入 `CLARIFY`，不会形成模型循环。
- M2 只创建并暂停高风险审批；审批身份、一次性 decision token 和拒绝语义留给 M3。
- provider timeout/429/5xx 的退避与副作用 outbox 留给 M4。
