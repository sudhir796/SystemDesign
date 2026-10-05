"""Domain models for inventory management."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Inventory:
    """Represents product inventory with stock conservation guarantees."""
    inventory_id: str
    product_id: str
    total_stock: int
    available_quantity: int
    reserved_quantity: int = 0
    sold_quantity: int = 0
    version: int = 0
    updated_at: Optional[str] = None

    def validate_conservation(self) -> bool:
        """Validates that stock quantities are non-negative and sum to total stock."""
        return (
            self.available_quantity >= 0
            and self.reserved_quantity >= 0
            and self.sold_quantity >= 0
            and (self.available_quantity + self.reserved_quantity + self.sold_quantity) == self.total_stock
        )
