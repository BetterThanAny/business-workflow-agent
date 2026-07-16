# ADR 0005: Deterministic evaluation and replaceable telemetry

## Status

Accepted for M5 on 2026-07-16.

## Decision

1. Keep the M5 scenario set as a hand-inspectable, immutable JSONL snapshot with a
   duplicated routing key (`task_type`) that is validated on load.
2. Evaluate every case through the real runner, policy, schemas, outbox, and database
   using deterministic test doubles. Use a new in-memory database per case to prevent
   ordering leakage and production mutation.
3. Treat task state, preferred tool, exact arguments, steps, permission violations,
   duplicate side effects, and orchestration latency as deterministic metrics. A
   permission bypass or duplicate side effect has zero tolerance.
4. Integrate with the separately versioned `llm-eval-platform` over authenticated
   HTTP/JSON. Do not add a filesystem path dependency because the projects pin
   different Python minor versions and must remain deployable independently.
5. Keep Prometheus collectors and the OpenTelemetry provider per application instance.
   This avoids global test contamination and keeps exporters injectable.
6. Measure the full local deterministic workflow duration as a conservative upper
   bound for orchestration overhead. Because it includes local stub and tool time,
   passing the 200 ms bound also implies the strictly excluded value passes.

## Consequences

The default suite is reproducible and makes no paid or remote calls. Failed cases
preserve complete redacted trajectories. Production observability still requires an
external collector, retention policy, network protection for `/metrics`, and an
opt-in live-provider suite; those deployment choices do not change policy behavior.
