"""Order service interface and mock implementation with downtime simulation."""

from abc import ABC, abstractmethod
import sqlite3
import time
from typing import Optional
import uuid

from app.config import Config
from app.db import get_db
from app.models.order import Order
from app.models.order_state import OrderStatus
from app.repositories.order_repo import OrderRepository


class OrderServiceDownError(Exception):
    """Raised when the Order Service is currently simulated as DOWN."""
    pass


class OrderService(ABC):
    """Abstract interface for Order Service providers."""

    @abstractmethod
    def create_order(self, reservation_id: str, customer_id: str) -> Order:
        """Creates an order idempotently for a confirmed reservation."""
        pass


class MockOrderService(OrderService):
    """Mock implementation of OrderService with simulated downtime capabilities."""

    def __init__(
        self,
        order_repo: Optional[OrderRepository] = None,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path or Config.db_path()
        self.order_repo = order_repo or OrderRepository(db_path=self.db_path)
        self.is_manually_down: bool = False
        self.down_until_timestamp: float = 0.0

    def simulate_down_for(self, seconds: float = 30.0) -> None:
        """Simulate being down for the given number of seconds."""
        self.down_until_timestamp = time.time() + seconds

    def set_down(self, is_down: bool) -> None:
        """Manually toggle service health."""
        self.is_manually_down = is_down
        if not is_down:
            self.down_until_timestamp = 0.0

    def is_available(self) -> bool:
        if self.is_manually_down:
            return False
        if time.time() < self.down_until_timestamp:
            return False
        return True

    def create_order(self, reservation_id: str, customer_id: str) -> Order:
        """Idempotently creates an order record.
        
        If the service is simulated as DOWN, raises OrderServiceDownError.
        If an order with reservation_id already exists, returns the existing order.
        """
        if not self.is_available():
            raise OrderServiceDownError("OrderService is temporarily unavailable (503 Service Unavailable).")

        # 1. Idempotency check: Return existing order if already created
        existing = self.order_repo.get_by_reservation_id(reservation_id)
        if existing is not None:
            return existing

        with get_db(self.db_path) as conn:
            # Recheck under transaction
            existing = self.order_repo.get_by_reservation_id(reservation_id, conn=conn)
            if existing is not None:
                return existing

            order = Order(
                order_id=f"ord_{uuid.uuid4().hex[:12]}",
                reservation_id=reservation_id,
                customer_id=customer_id,
                status=OrderStatus.CONFIRMED.value,
            )
            try:
                created = self.order_repo.create(order, conn=conn)
                return created
            except sqlite3.IntegrityError:
                # Race condition: duplicate reservation_id inserted concurrently
                existing = self.order_repo.get_by_reservation_id(reservation_id)
                if existing:
                    return existing
                raise


_default_mock_order_service: Optional[MockOrderService] = None


def get_mock_order_service(db_path: Optional[str] = None) -> MockOrderService:
    """Returns a shared MockOrderService instance."""
    global _default_mock_order_service
    if _default_mock_order_service is None or (db_path and _default_mock_order_service.db_path != db_path):
        _default_mock_order_service = MockOrderService(db_path=db_path)
    return _default_mock_order_service

