"""Repositories package."""

from typing import Optional
from app.config import Config
from app.repositories.inventory_repo import (
    InventoryRepository,
    SqliteInventoryRepository,
)
from app.repositories.pessimistic_repo import PessimisticInventoryRepository
from app.repositories.reservation_repo import ReservationRepository
from app.repositories.payment_repo import PaymentRepository
from app.repositories.order_repo import OrderRepository
from app.repositories.outbox_repo import OutboxRepository


def get_inventory_repository(
    strategy: Optional[str] = None, db_path: Optional[str] = None
) -> InventoryRepository:
    """Factory selecting InventoryRepository based on config flag or argument."""
    selected_strategy = (strategy or Config.inventory_strategy()).lower()
    target_db = db_path or Config.db_path()
    if selected_strategy == "pessimistic":
        return PessimisticInventoryRepository(db_path=target_db)
    return SqliteInventoryRepository(db_path=target_db)


__all__ = [
    "InventoryRepository",
    "SqliteInventoryRepository",
    "PessimisticInventoryRepository",
    "ReservationRepository",
    "PaymentRepository",
    "OrderRepository",
    "OutboxRepository",
    "get_inventory_repository",
]
