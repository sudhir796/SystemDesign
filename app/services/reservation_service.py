"""Reservation service orchestrating atomic stock reservation and reservation creation."""

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Optional, Tuple
import uuid

from app.config import Config
from app.db import get_db
from app.models.reservation import InventoryReservation, ReservationStatus
from app.repositories import (
    InventoryRepository,
    ReservationRepository,
    get_inventory_repository,
)


class ReservationService:
    """Orchestrates stock reservation and reservation records within a single transaction."""

    def __init__(
        self,
        inventory_repo: Optional[InventoryRepository] = None,
        reservation_repo: Optional[ReservationRepository] = None,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path or Config.db_path()
        self.inventory_repo = inventory_repo or get_inventory_repository(db_path=self.db_path)
        self.reservation_repo = reservation_repo or ReservationRepository(db_path=self.db_path)

    def reserve(
        self,
        product_id: str,
        customer_id: str,
        idempotency_key: str,
        ttl_seconds: Optional[int] = None,
    ) -> Tuple[Optional[InventoryReservation], bool]:
        """Reserves 1 unit of stock for a customer.
        
        Returns:
            (reservation, is_new):
            - (reservation, True) if newly created (HTTP 201)
            - (reservation, False) if already existed (idempotent replay, HTTP 200)
            - (None, False) if sold out (available_quantity == 0, HTTP 409)
            
        Concurrency & Idempotency Guarantees:
        - Checks idempotency before stock deduction.
        - Relies on UNIQUE constraint on idempotency_key to catch race conditions
          between identical concurrent requests.
        - On UNIQUE violation, transaction rolls back (preventing double stock deduction)
          and the winning existing record is returned.
        """
        # 1. Fast-path check: Return existing reservation if already processed
        existing = self.reservation_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing, False

        try:
            with get_db(self.db_path) as conn:
                # Re-check under connection in case it was created just now
                existing = self.reservation_repo.get_by_idempotency_key(idempotency_key, conn=conn)
                if existing is not None:
                    return existing, False

                # 2. Atomic stock reservation (Never read-then-write in application code)
                reserved = self.inventory_repo.reserve_stock(
                    product_id=product_id, quantity=1, conn=conn
                )
                if not reserved:
                    # Stock is 0 or product not found -> Sold out
                    return None, False

                # 3. Insert reservation row in the SAME transaction
                effective_ttl = ttl_seconds if ttl_seconds is not None else Config.reservation_ttl_seconds()
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=effective_ttl)
                ).strftime("%Y-%m-%d %H:%M:%S")

                reservation = InventoryReservation(
                    reservation_id=f"res_{uuid.uuid4().hex[:12]}",
                    product_id=product_id,
                    customer_id=customer_id,
                    idempotency_key=idempotency_key,
                    status=ReservationStatus.RESERVED.value,
                    expires_at=expires_at,
                )
                
                created = self.reservation_repo.create(reservation, conn=conn)
                return created, True

        except sqlite3.IntegrityError:
            # 4. Handle identical concurrent requests racing each other:
            # UNIQUE constraint on idempotency_key failed. The transaction rolled back automatically,
            # ensuring stock was NOT decremented by this losing request.
            # Now fetch and return the winning reservation created by the concurrent request.
            existing = self.reservation_repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing, False
            # If not caused by idempotency key (e.g. check constraint), re-raise
            raise
