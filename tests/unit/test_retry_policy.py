from business_workflow_agent.workflow.retry import RetryPolicy


def test_retry_policy_uses_bounded_exponential_backoff_with_jitter() -> None:
    no_jitter = RetryPolicy(
        base_delay_seconds=2,
        max_delay_seconds=10,
        jitter_ratio=0.25,
        random_value=lambda: 0.0,
    )
    max_jitter = RetryPolicy(
        base_delay_seconds=2,
        max_delay_seconds=10,
        jitter_ratio=0.25,
        random_value=lambda: 1.0,
    )

    assert [no_jitter.delay_seconds(attempt) for attempt in range(1, 6)] == [
        2,
        4,
        8,
        10,
        10,
    ]
    assert max_jitter.delay_seconds(1) == 2.5
    assert max_jitter.delay_seconds(4) == 10
