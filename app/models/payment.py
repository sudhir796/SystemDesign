"""Domain models for payments."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Payment:
    """Represents a payment attempt or record."""
    payment_id: str
    reservation_id: str
    idempotency_key: str
    status: str
    gateway_ref: Optional[str] = None
    created_at: Optional[str] = None
