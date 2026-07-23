# ADR 0007: Unified high-risk approval and opt-in live evidence

## Status

Accepted for M7 on 2026-07-23.

## Decision

1. Route every `WRITE_HIGH_RISK` request through the same persisted approval record,
   including the REST refund endpoint. Record whether the request originated from an
   Agent tool call or the direct API; origin changes continuation behavior, never policy.
2. Keep deterministic providers as the reproducible default and require explicit
   `openai_compatible` configuration for live HTTP. Live errors never fall back to a stub.
3. Keep the 160-case deterministic release gate separate from the balanced 84-case
   opt-in live matrix. Publish actual live failures and quality without turning them into
   authorization evidence.
4. Exercise enterprise-rag with a real HTTP service and Redis. Missing live resources
   remain `unverified`; protocol stubs cannot upgrade that status.

## Consequences

`POST /api/v1/refunds` now returns `202 APPROVAL_REQUIRED` rather than creating a
refund immediately. Existing clients must complete the independent decision-token flow.
Default CI remains credential-free while enforcing deterministic coverage and secret gates.
