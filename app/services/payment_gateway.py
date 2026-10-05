"""Payment Gateway interface and mock implementation."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import random
from typing import Dict, Optional
import uuid


class PaymentGatewayStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"


@dataclass
class PaymentGatewayResult:
    status: PaymentGatewayStatus
    gateway_ref: Optional[str] = None
    error_message: Optional[str] = None


class PaymentGateway(ABC):
    """Abstract interface for payment gateway providers."""

    @abstractmethod
    def charge(self, idempotency_key: str, amount: float = 100.0) -> PaymentGatewayResult:
        """Charges a customer using an idempotency key. Never double-charges."""
        pass

    @abstractmethod
    def check_status(self, idempotency_key: str) -> Optional[PaymentGatewayResult]:
        """Queries the gateway status for a previous charge attempt by idempotency key."""
        pass


class MockPaymentGateway(PaymentGateway):
    """Mock payment gateway with configurable success, failure, and timeout probabilities.
    
    Default: 95% success, 5% failure, configurable timeouts.
    Maintains an internal idempotent ledger so queries for the same idempotency key
    return deterministic results without charging twice.
    """

    def __init__(
        self,
        success_rate: float = 0.95,
        failure_rate: float = 0.05,
        timeout_rate: float = 0.0,
    ):
        self.success_rate = success_rate
        self.failure_rate = failure_rate
        self.timeout_rate = timeout_rate
        # Ledger maps idempotency_key -> PaymentGatewayResult
        self._ledger: Dict[str, PaymentGatewayResult] = {}
        # Tracks the intended actual resolution for timed out transactions
        self._reconciliation_outcomes: Dict[str, PaymentGatewayResult] = {}

    def charge(self, idempotency_key: str, amount: float = 100.0) -> PaymentGatewayResult:
        """Process charge with idempotency protection and simulated probabilistic outcomes."""
        if idempotency_key in self._ledger:
            return self._ledger[idempotency_key]

        rand = random.random()
        
        # Check timeout probability first
        if rand < self.timeout_rate:
            result = PaymentGatewayResult(
                status=PaymentGatewayStatus.TIMEOUT,
                error_message="Gateway timeout: Request timed out before acknowledgment",
            )
            # Decide what the backend actually ended up doing (preserve pre-configured outcomes if set)
            if idempotency_key not in self._reconciliation_outcomes:
                resolved_status = (
                    PaymentGatewayStatus.SUCCESS if random.random() <= 0.95 else PaymentGatewayStatus.FAILURE
                )
                self._reconciliation_outcomes[idempotency_key] = PaymentGatewayResult(
                    status=resolved_status,
                    gateway_ref=f"gw_tx_{uuid.uuid4().hex[:10]}" if resolved_status == PaymentGatewayStatus.SUCCESS else None,
                )
        elif rand < (self.timeout_rate + self.failure_rate):
            result = PaymentGatewayResult(
                status=PaymentGatewayStatus.FAILURE,
                error_message="Payment declined: Insufficient funds or invalid card",
            )
        else:
            result = PaymentGatewayResult(
                status=PaymentGatewayStatus.SUCCESS,
                gateway_ref=f"gw_tx_{uuid.uuid4().hex[:10]}",
            )

        self._ledger[idempotency_key] = result
        return result

    def check_status(self, idempotency_key: str) -> Optional[PaymentGatewayResult]:
        """Used during reconciliation to resolve UNKNOWN / TIMEOUT transactions."""
        if idempotency_key in self._reconciliation_outcomes:
            return self._reconciliation_outcomes[idempotency_key]
        return self._ledger.get(idempotency_key)


_default_mock_payment_gateway: Optional[MockPaymentGateway] = None


def get_default_payment_gateway() -> MockPaymentGateway:
    """Returns a shared singleton instance of MockPaymentGateway."""
    global _default_mock_payment_gateway
    if _default_mock_payment_gateway is None:
        _default_mock_payment_gateway = MockPaymentGateway()
    return _default_mock_payment_gateway
