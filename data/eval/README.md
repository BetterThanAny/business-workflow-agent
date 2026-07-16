# M5 agent scenario set

`agent_cases.jsonl` is the immutable `m5-v1` scenario snapshot. It contains 160
cases, 20 for each required family: knowledge Q&A, missing parameters, multi-tool,
authorization denial, prompt injection, provider timeout/recovery, approval pause,
and replay/idempotency.

Every row uses the default `llm-eval-platform` JSONL mapping:

- `id` -> external case ID
- `input` -> target input
- `expected_output` -> deterministic expected business evidence
- `tags`, `language`, `difficulty`, `task_type` -> evaluation slices

The platform target is the authenticated HTTP adapter in
`config/llm-eval-platform-target.json`. Put a short-lived Admin bearer token in
`EVAL_AUTHORIZATION` as the full value `Bearer <token>`; the token is never stored in
the config or dataset. The target runs each case in an isolated, in-memory database
using the deterministic provider, so it cannot mutate production business data or
call a paid provider.

The target output matches the platform's agent evaluators: `final_state`,
`tool_calls`, `token_count`, `side_effects`, `approval_ids`, and `recovered`.
Expected-output fields are supplied as evaluator metadata when registering
`AgentTaskSuccessEvaluator`, `FirstToolAccuracyEvaluator`,
`ToolParameterAccuracyEvaluator`, `PermissionViolationEvaluator`, and
`DuplicateSideEffectEvaluator`.
