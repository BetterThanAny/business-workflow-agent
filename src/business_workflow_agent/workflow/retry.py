import random
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.25
    random_value: Callable[[], float] = field(default=random.random, repr=False)

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0:
            raise ValueError("base delay must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("maximum delay must not be below base delay")
        if self.jitter_ratio < 0:
            raise ValueError("jitter ratio must not be negative")

    def delay_seconds(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        base = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** (attempt - 1)),
        )
        random_fraction = min(1.0, max(0.0, self.random_value()))
        jitter = base * self.jitter_ratio * random_fraction
        return min(self.max_delay_seconds, base + jitter)
