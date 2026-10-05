"""State pattern implementation for Order lifecycle management."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Set


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    CONFIRMED = "CONFIRMED"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"


class InvalidStateTransitionError(Exception):
    """Raised when an illegal order state transition is attempted."""
    pass


class OrderState(ABC):
    """Abstract state in the Order State Machine."""

    @property
    @abstractmethod
    def status(self) -> OrderStatus:
        pass

    @property
    @abstractmethod
    def allowed_transitions(self) -> Set[OrderStatus]:
        pass

    def transition_to(self, next_status: OrderStatus) -> "OrderState":
        """Validates and returns the new state object or raises InvalidStateTransitionError."""
        if next_status not in self.allowed_transitions:
            raise InvalidStateTransitionError(
                f"Illegal order transition: Cannot transition from {self.status.value} to {next_status.value}."
            )
        return get_order_state(next_status)


class CreatedState(OrderState):
    status = OrderStatus.CREATED
    allowed_transitions = {OrderStatus.PAYMENT_PENDING, OrderStatus.CONFIRMED}


class PaymentPendingState(OrderState):
    status = OrderStatus.PAYMENT_PENDING
    allowed_transitions = {OrderStatus.CONFIRMED}


class ConfirmedState(OrderState):
    status = OrderStatus.CONFIRMED
    allowed_transitions = {OrderStatus.PROCESSING}


class ProcessingState(OrderState):
    status = OrderStatus.PROCESSING
    allowed_transitions = {OrderStatus.SHIPPED}


class ShippedState(OrderState):
    status = OrderStatus.SHIPPED
    allowed_transitions = {OrderStatus.OUT_FOR_DELIVERY}


class OutForDeliveryState(OrderState):
    status = OrderStatus.OUT_FOR_DELIVERY
    allowed_transitions = {OrderStatus.DELIVERED}


class DeliveredState(OrderState):
    status = OrderStatus.DELIVERED
    allowed_transitions = set()  # Terminal state


_STATE_MAP = {
    OrderStatus.CREATED: CreatedState,
    OrderStatus.PAYMENT_PENDING: PaymentPendingState,
    OrderStatus.CONFIRMED: ConfirmedState,
    OrderStatus.PROCESSING: ProcessingState,
    OrderStatus.SHIPPED: ShippedState,
    OrderStatus.OUT_FOR_DELIVERY: OutForDeliveryState,
    OrderStatus.DELIVERED: DeliveredState,
}


def get_order_state(status: OrderStatus | str) -> OrderState:
    """Factory returning the state instance corresponding to an OrderStatus."""
    if isinstance(status, str):
        status = OrderStatus(status)
    state_cls = _STATE_MAP.get(status)
    if not state_cls:
        raise ValueError(f"Unknown order status: {status}")
    return state_cls()
