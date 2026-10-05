"""Domain models for orders."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Order:
    """Represents a finalized customer order."""
    order_id: str
    reservation_id: str
    customer_id: str
    status: str
    created_at: Optional[str] = None
