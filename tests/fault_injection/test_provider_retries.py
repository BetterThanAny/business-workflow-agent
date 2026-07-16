from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.db import create_session_factory
from business_workflow_agent.models import WorkflowCheckpoint
from business_workflow_agent.schemas import AgentRunCreateInput, AgentRunResumeInput
from business_workflow_agent.tools.registry import build_tool_registry
from business_workflow_agent.workflow.provider import (
    DeterministicProvider,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from business_workflow_agent.workflow.retry import RetryPolicy
from business_workflow_agent.workflow.runner import AgentRunner, WorkflowResumeError


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 16, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FailingThenHealthyProvider(DeterministicProvider):
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.classify_calls = 0

    def classify(self, request: object):  # type: ignore[no-untyped-def]
        self.classify_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return super().classify(request)  # type: ignore[arg-type]


def _runner(
    engine: object,
    provider: FailingThenHealthyProvider,
    clock: MutableClock,
) -> AgentRunner:
    return AgentRunner(
        create_session_factory(engine),
        build_tool_registry(),
        provider,
        clock=clock,
        retry_policy=RetryPolicy(
            base_delay_seconds=2,
            max_delay_seconds=8,
            jitter_ratio=0,
            random_value=lambda: 0,
        ),
        max_provider_retries=3,
    )


def test_429_timeout_and_5xx_resume_after_persisted_backoff(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    clock = MutableClock()
    provider = FailingThenHealthyProvider(
        [
            ProviderRateLimitError(),
            ProviderTimeoutError(),
            ProviderServerError(503),
        ]
    )
    runner = _runner(engine, provider, clock)
    support = principal_factory(Role.SUPPORT_AGENT)
    run = runner.create(
        support,
        AgentRunCreateInput(message="search knowledge", context={"query": "MFA"}),
    )
    runner.advance_once(support, run.id)

    first = runner.advance_once(support, run.id)
    assert first.state == "RETRYABLE_FAILURE"
    assert first.error_code == "PROVIDER_RATE_LIMIT"
    assert first.retry_count == 1
    assert first.next_retry_at == clock.now + timedelta(seconds=2)
    with pytest.raises(WorkflowResumeError, match="backoff"):
        runner.resume(support, run.id, AgentRunResumeInput())

    clock.advance(2)
    second = runner.resume(support, run.id, AgentRunResumeInput())
    assert second.state == "RETRYABLE_FAILURE"
    assert second.error_code == "PROVIDER_TIMEOUT"
    assert second.retry_count == 2
    assert second.next_retry_at == clock.now + timedelta(seconds=4)

    clock.advance(4)
    third = runner.resume(support, run.id, AgentRunResumeInput())
    assert third.state == "RETRYABLE_FAILURE"
    assert third.error_code == "PROVIDER_SERVER_503"
    assert third.retry_count == 3
    assert third.next_retry_at == clock.now + timedelta(seconds=8)

    clock.advance(8)
    restarted = _runner(engine, provider, clock)
    completed = restarted.resume(support, run.id, AgentRunResumeInput())
    assert completed.state == "COMPLETE"
    assert completed.retry_count == 0
    assert provider.classify_calls == 4

    with create_session_factory(engine)() as session:
        states = list(
            session.scalars(
                select(WorkflowCheckpoint.state)
                .where(WorkflowCheckpoint.run_id == run.id)
                .order_by(WorkflowCheckpoint.version)
            )
        )
    assert states.count("RETRYABLE_FAILURE") == 3
    assert states.count("RETRY") == 3


def test_non_retryable_provider_error_and_retry_exhaustion_terminate_stably(
    engine: object,
    principal_factory: Callable[..., Principal],
) -> None:
    clock = MutableClock()
    support = principal_factory(Role.SUPPORT_AGENT)
    malformed_provider = FailingThenHealthyProvider([ProviderMalformedOutputError()])
    malformed_runner = _runner(engine, malformed_provider, clock)
    malformed = malformed_runner.create(
        support,
        AgentRunCreateInput(message="search knowledge", context={"query": "MFA"}),
    )
    malformed_result = malformed_runner.run_to_pause(support, malformed.id)
    malformed_replay = malformed_runner.run_to_pause(support, malformed.id)
    assert malformed_result.state == "NON_RETRYABLE_FAILURE"
    assert malformed_result.error_code == "PROVIDER_MALFORMED_OUTPUT"
    assert malformed_replay.version == malformed_result.version
    assert malformed_provider.classify_calls == 1

    exhausted_provider = FailingThenHealthyProvider(
        [ProviderRateLimitError() for _ in range(4)]
    )
    exhausted_runner = _runner(engine, exhausted_provider, clock)
    exhausted = exhausted_runner.create(
        support,
        AgentRunCreateInput(message="search knowledge", context={"query": "MFA"}),
    )
    result = exhausted_runner.run_to_pause(support, exhausted.id)
    for delay in (2, 4, 8):
        assert result.state == "RETRYABLE_FAILURE"
        clock.advance(delay)
        result = exhausted_runner.resume(support, exhausted.id, AgentRunResumeInput())
    assert result.state == "NON_RETRYABLE_FAILURE"
    assert result.error_code == "PROVIDER_RETRIES_EXHAUSTED"
    assert exhausted_provider.classify_calls == 4
    stable = exhausted_runner.run_to_pause(support, exhausted.id)
    assert stable.version == result.version
