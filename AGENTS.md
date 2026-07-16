# AGENTS.md

## Scope

These instructions apply to the entire `business-workflow-agent` project.

This project demonstrates safe LLM integration with business systems. Prefer explicit state, policy enforcement, idempotent side effects, approvals, and auditability over autonomous or conversational complexity.

## Source of truth

- `PLAN.md` defines scope, milestones, acceptance criteria, tests, and risks.
- Keep `PLAN.md` updated when milestone status, acceptance commands, architecture, or installed tools change.
- A workflow feature is not complete until happy path, denial path, retry path, and replay behavior have been tested.
- Record durable design decisions under `docs/adr/` once available.

## Required workflow

1. Inspect repository state and read the relevant domain, policy, workflow, and tests.
2. Identify the smallest coherent milestone slice.
3. Add a failing regression test for policy, state transition, schema, or side-effect behavior.
4. Implement the change without broadening tool permissions.
5. Run focused tests, then lint, type checks, integration, security, and fault tests as appropriate.
6. Report verified, unverified, and non-finding results separately.

## Security and authorization invariants

- The LLM may propose an action; it may never authorize an action.
- Authorization is enforced inside the tool executor using authenticated server-side identity and scopes.
- Model text, conversation memory, tool arguments, and retrieved documents are untrusted input.
- High-risk tools must always pause at a persisted human approval state.
- Approval identity must be separate from the Agent run and must be verified server-side.
- Approval rejection is terminal for that proposed high-risk call unless a new proposal is created.
- Never expose raw SQL execution as an LLM tool.
- Never dynamically import or execute a tool named by model output.
- Only registered tools with fixed schemas may execute.
- Tool input and output logs must be redacted before persistence or export.

## Workflow invariants

- Every run has a stable run ID, tenant ID, user ID, state, version, and budget.
- State transitions are explicit, validated, and persisted.
- Every external side effect uses an idempotency key.
- Checkpoint replay must not repeat a completed side effect.
- Retryable and non-retryable errors must be distinct.
- Retries use bounded exponential backoff with jitter.
- Cancellation prevents any pending write tool from starting.
- Maximum steps, tool calls, elapsed time, tokens, and cost are enforced by code.
- Unknown or ambiguous inputs move to clarification or manual review, not guessed execution.

## Tool design

Each tool must define:

- stable name and version;
- Pydantic input and output schemas;
- risk class: `READ_ONLY`, `WRITE_LOW_RISK`, or `WRITE_HIGH_RISK`;
- required role/scope;
- timeout and retry policy;
- idempotency behavior;
- PII redaction policy;
- audit event fields;
- deterministic test double.

Core domain, policy, and tool execution code must not depend on LangGraph-specific types. Framework adapters should remain replaceable.

## Environment and dependencies

- Pin Python and tools with `mise`.
- Add Python dependencies with `uv add`; run commands through `uv run`.
- Run PostgreSQL and Redis in Docker/OrbStack, not as Homebrew services.
- Do not install global packages without user approval.
- Do not use `direnv`.
- Keep project variables in `.mise.toml`; load `.env` with `_.file = ".env"` where needed.
- Never commit secrets; use environment variables or `op://...` references.

## Database and audit rules

- Schema changes require Alembic migrations and migration tests.
- Use database constraints for idempotency and state uniqueness where possible.
- Side-effect execution records and audit events are append-only in normal operation.
- Audit records must link user, tenant, run, state, tool, redacted arguments, result, and timestamp.
- Do not delete audit history as part of ordinary business object deletion.
- Destructive migrations, audit pruning, or history rewriting require explicit approval.

## Evaluation rules

- Maintain a versioned scenario set with happy, ambiguous, adversarial, timeout, replay, approval, and denial cases.
- Task success requires correct business state, not merely a plausible final answer.
- Tool-choice accuracy does not substitute for authorization and side-effect checks.
- Permission bypasses and duplicate side effects are zero-tolerance release gates.
- Preserve full tool trajectory for failed cases.
- LLM-as-judge may supplement but cannot replace deterministic policy and side-effect assertions.

## Testing and verification

When implemented, the expected verification sequence is:

```bash
mise exec -- uv run ruff check .
mise exec -- uv run pyright
mise exec -- uv run pytest -q
mise exec -- uv run pytest -q tests/integration
mise exec -- uv run pytest -q tests/security
mise exec -- uv run pytest -q tests/fault_injection
mise exec -- uv run python scripts/smoke_workflow.py
```

Tests must include:

- exact schema validation;
- role/scope denial and cross-tenant denial;
- prompt injection and malicious tool arguments;
- duplicate delivery and checkpoint replay;
- process termination at every persisted state;
- approval approve/reject/expired/already-used cases;
- provider timeout, 429, 5xx, malformed output, and budget exhaustion;
- cancellation before and during tool execution.

Paid or remote providers must be stubbed in the default suite. A separate opt-in live suite may exercise real providers.

## Privacy and Git hygiene

- Do not hardcode user home paths in source or reusable configuration.
- Use placeholder identities such as `user@example.com` in examples and fixtures.
- Never include personal email addresses or AI co-author trailers.
- Run `gitleaks detect` before commits or exports if prompts, traces, `.env`, or business fixtures might contain secrets.
- Ask before force-pushes, destructive deletion, CI permission changes, publishing, or external/public messages.
