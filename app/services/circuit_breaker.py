"""Circuit Breaker pattern for protecting downstream services."""

from enum import Enum
import time
from typing import Callable, Any


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Raised when an operation is attempted while the circuit is OPEN."""
    pass


class CircuitBreaker:
    """Simple Circuit Breaker to prevent cascading failures to downstream services."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 5.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0.0

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Executes func wrapped with circuit breaker protection."""
        now = time.time()

        # Check if circuit can transition from OPEN to HALF_OPEN
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout_seconds:
                self.state = CircuitState.HALF_OPEN
            else:
                raise CircuitBreakerOpenError("Circuit is OPEN. Fast-failing downstream call.")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e

    def _on_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold or self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
