from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


class WorkflowTelemetry:
    """Per-application telemetry without process-global metric state."""

    def __init__(self, *, span_exporter: SpanExporter | None = None) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self._provider = TracerProvider(
            resource=Resource.create({"service.name": "business-workflow-agent"})
        )
        if span_exporter is not None:
            self._provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        self.tracer = self._provider.get_tracer("business_workflow_agent", "0.1.0")
        self.runs_created = Counter(
            "business_workflow_runs_created",
            "Agent workflow runs created by server-side role.",
            ("role",),
            registry=self.registry,
        )
        self.transitions = Counter(
            "business_workflow_state_transitions",
            "Persisted workflow state transitions.",
            ("source", "target"),
            registry=self.registry,
        )
        self.llm_requests = Counter(
            "business_workflow_llm_requests",
            "Structured provider requests.",
            ("operation", "status"),
            registry=self.registry,
        )
        self.tool_executions = Counter(
            "business_workflow_tool_executions",
            "Registered tool execution outcomes.",
            ("tool", "status"),
            registry=self.registry,
        )
        self.orchestration_duration = Histogram(
            "business_workflow_orchestration_duration_milliseconds",
            "Conservative workflow orchestration latency including deterministic local stubs.",
            buckets=(1, 2.5, 5, 10, 25, 50, 100, 200, 500, 1000),
            registry=self.registry,
        )

    @contextmanager
    def span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Generator[Span, None, None]:
        with self.tracer.start_as_current_span(name, attributes=attributes or {}) as span:
            yield span

    def record_run_created(self, roles: frozenset[object]) -> None:
        role = ",".join(sorted(str(value) for value in roles)) or "internal"
        self.runs_created.labels(role=role).inc()

    def record_transition(self, source: str, target: str) -> None:
        self.transitions.labels(source=source, target=target).inc()

    def record_llm(self, operation: str, status: str) -> None:
        self.llm_requests.labels(operation=operation, status=status).inc()

    def record_tool(self, tool: str, status: str) -> None:
        self.tool_executions.labels(tool=tool, status=status).inc()

    def observe_orchestration(self, duration_ms: float) -> None:
        self.orchestration_duration.observe(duration_ms)

    def prometheus_payload(self) -> bytes:
        return generate_latest(self.registry)

    def shutdown(self) -> None:
        self._provider.shutdown()


def safe_span_attributes(**values: object) -> dict[str, str | int | float | bool]:
    """Keep only bounded, non-PII telemetry attributes."""

    attributes: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        if isinstance(value, (str, int, float, bool)):
            attributes[key] = value
    return attributes
