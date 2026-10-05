"""Outbox worker that polls pending events and creates orders via OrderService."""

import json
import logging
from typing import Optional
from app.config import Config
from app.db import get_connection
from app.repositories import OutboxRepository
from app.services.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.services.order_service import OrderService, get_mock_order_service

logger = logging.getLogger(__name__)


class OutboxWorker:
    """Asynchronously processes outbox events with retries, exponential backoff, dead-lettering, and circuit breaker."""

    def __init__(
        self,
        order_service: Optional[OrderService] = None,
        outbox_repo: Optional[OutboxRepository] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        max_attempts: int = 5,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path or Config.db_path()
        self.outbox_repo = outbox_repo or OutboxRepository(db_path=self.db_path)
        self.order_service = order_service or get_mock_order_service(db_path=self.db_path)
        self.circuit_breaker = circuit_breaker or CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=5.0)
        self.max_attempts = max_attempts

    def process_batch(self, limit: int = 50) -> int:
        """Processes a batch of pending outbox events.
        
        Returns the number of events successfully published.
        """
        pending_events = self.outbox_repo.get_pending(limit=limit)
        successful_count = 0

        for event in pending_events:
            try:
                payload = json.loads(event.payload)
                reservation_id = payload["reservation_id"]
                customer_id = payload["customer_id"]

                # Dispatch call wrapped with Circuit Breaker
                self.circuit_breaker.call(
                    self.order_service.create_order,
                    reservation_id=reservation_id,
                    customer_id=customer_id,
                )

                # Order successfully created -> mark PUBLISHED
                self.outbox_repo.update_status(event.event_id, "PUBLISHED")
                successful_count += 1

            except CircuitBreakerOpenError:
                # Circuit is currently OPEN: Do not burn retry attempts while circuit is cooling down.
                # Stop processing remaining batch to allow downstream service recovery.
                logger.info("Circuit breaker is OPEN. Halting outbox batch processing until recovery.")
                break

            except Exception as err:
                # Downstream call was attempted and failed: Increment attempt counter
                self.outbox_repo.increment_attempts(event.event_id)
                current_attempts = event.attempts + 1
                logger.warning(
                    f"Failed to process outbox event {event.event_id} (Attempt {current_attempts}/{self.max_attempts}): {err}"
                )
                if current_attempts >= self.max_attempts:
                    # Exceeded maximum retry attempts -> Move to DEAD_LETTER
                    self.outbox_repo.update_status(event.event_id, "DEAD_LETTER")

        return successful_count

    def retry_dead_letter(self) -> int:
        """Resets DEAD_LETTER events back to PENDING for manual or post-outage recovery."""
        conn = get_connection(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE outbox_event
                SET status = 'PENDING', attempts = 0
                WHERE status = 'DEAD_LETTER'
                """
            )
            count = cursor.rowcount
            conn.commit()
            return count
        finally:
            conn.close()
