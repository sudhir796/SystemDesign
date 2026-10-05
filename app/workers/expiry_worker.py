"""Background worker that detects and expires stale reservations, returning stock to inventory."""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional

from app.config import Config
from app.db import get_db
from app.repositories import (
    InventoryRepository,
    ReservationRepository,
    get_inventory_repository,
)

logger = logging.getLogger(__name__)


class ExpiryWorker:
    """Finds expired reservations and atomically transitions status=EXPIRED and stock reserved->available.
    
    Guarantees:
    - Worker-safety: If two workers run simultaneously, they use conditional updates
      (WHERE status IN ('RESERVED', 'PAYMENT_PENDING')) and check row count to ensure
      each expired unit is returned to inventory exactly once.
    - Single transaction per expired item (or batch).
    """

    def __init__(
        self,
        inventory_repo: Optional[InventoryRepository] = None,
        reservation_repo: Optional[ReservationRepository] = None,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path or Config.db_path()
        self.inventory_repo = inventory_repo or get_inventory_repository(db_path=self.db_path)
        self.reservation_repo = reservation_repo or ReservationRepository(db_path=self.db_path)

    def process_expired_batch(self, limit: int = 100) -> int:
        """Finds reservations whose expires_at < now and expires them atomically.
        
        Returns the number of reservations expired and returned to available stock.
        """
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        candidates = self.reservation_repo.get_expired(now_str, limit=limit)
        if not candidates:
            return 0

        expired_count = 0
        for candidate in candidates:
            with get_db(self.db_path) as conn:
                # 1. Atomic conditional update on reservation status
                # Safe if multiple workers process the same candidate simultaneously
                updated = self.reservation_repo.conditionally_expire(candidate.reservation_id, conn=conn)
                if not updated:
                    # Another worker or payment succeeded concurrently; skip returning stock
                    continue

                # 2. Return unit to inventory (reserved - 1, available + 1) in SAME transaction
                stock_released = self.inventory_repo.release_stock(candidate.product_id, quantity=1, conn=conn)
                if stock_released:
                    expired_count += 1
                    logger.info(
                        f"Expired reservation '{candidate.reservation_id}' and released stock for '{candidate.product_id}'."
                    )

        return expired_count

    async def run_loop(self, interval_seconds: float = 2.0, stop_event: Optional[asyncio.Event] = None) -> None:
        """Runs the expiry worker periodically until stop_event is set."""
        while stop_event is None or not stop_event.is_set():
            try:
                self.process_expired_batch()
            except Exception as e:
                logger.error(f"Error in ExpiryWorker loop: {e}", exc_info=True)
            await asyncio.sleep(interval_seconds)
