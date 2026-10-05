"""Domain models for inventory reservations."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ReservationStatus(str, Enum):
    RESERVED = "RESERVED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    CONFIRMED = "CONFIRMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


@dataclass
class InventoryReservation:
    """Represents a temporary stock reservation for a customer."""
    reservation_id: str
    product_id: str
    customer_id: str
    idempotency_key: str
    status: str
    expires_at: str
    created_at: Optional[str] = None
