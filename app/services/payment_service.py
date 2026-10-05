"""Payment service orchestrating payment processing, stock transitions, and transactional outbox."""

import json
import sqlite3
from typing import Optional, Tuple
import uuid

from app.config import Config
from app.db import get_db
from app.models.outbox import OutboxEvent
from app.models.payment import Payment
from app.models.reservation import ReservationStatus
from app.repositories import (
    InventoryRepository,
    PaymentRepository,
    ReservationRepository,
    OutboxRepository,
    get_inventory_repository,
)
from app.services.payment_gateway import (
    PaymentGateway,
    MockPaymentGateway,
    PaymentGatewayStatus,
    get_default_payment_gateway,
)


class ReservationNotFoundError(Exception):
    """Raised when the specified reservation_id is not found."""
    pass


class InvalidReservationStateError(Exception):
    """Raised when reservation is not in a valid state to be paid."""
    pass


class PaymentService:
    """Orchestrates payment gateway invocation and atomic state updates."""

    def __init__(
        self,
        gateway: Optional[PaymentGateway] = None,
        inventory_repo: Optional[InventoryRepository] = None,
        reservation_repo: Optional[ReservationRepository] = None,
        payment_repo: Optional[PaymentRepository] = None,
        outbox_repo: Optional[OutboxRepository] = None,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path or Config.db_path()
        self.gateway = gateway or get_default_payment_gateway()
        self.inventory_repo = inventory_repo or get_inventory_repository(db_path=self.db_path)
        self.reservation_repo = reservation_repo or ReservationRepository(db_path=self.db_path)
        self.payment_repo = payment_repo or PaymentRepository(db_path=self.db_path)
        self.outbox_repo = outbox_repo or OutboxRepository(db_path=self.db_path)

    def process_payment(
        self,
        reservation_id: str,
        idempotency_key: str,
    ) -> Tuple[Payment, bool]:
        """Processes payment for a reservation with strict idempotency and atomic updates.
        
        Returns:
            (Payment, is_new): Returns existing payment on idempotency replay.
        """
        # 1. Fast-path Idempotency Check: Same idempotency key must never create a second payment
        existing_payment = self.payment_repo.get_by_idempotency_key(idempotency_key)
        if existing_payment is not None:
            return existing_payment, False

        # 2. Pre-transaction validation of reservation
        reservation = self.reservation_repo.get_by_id(reservation_id)
        if reservation is None:
            raise ReservationNotFoundError(f"Reservation '{reservation_id}' not found.")

        if reservation.status != ReservationStatus.RESERVED.value and reservation.status != ReservationStatus.PAYMENT_PENDING.value:
            existing_payment = self.payment_repo.get_by_reservation_id(reservation_id)
            if existing_payment is not None:
                return existing_payment, False
            raise InvalidReservationStateError(
                f"Reservation '{reservation_id}' is in state '{reservation.status}' and cannot be paid."
            )

        # 3. Call Payment Gateway strictly OUTSIDE any database transaction
        result = self.gateway.charge(idempotency_key=idempotency_key)

        payment_id = f"pay_{uuid.uuid4().hex[:12]}"

        # 4. Open DB transaction only for writing the state changes
        with get_db(self.db_path) as conn:
            # Recheck idempotency inside transaction
            existing_payment = self.payment_repo.get_by_idempotency_key(idempotency_key, conn=conn)
            if existing_payment is not None:
                return existing_payment, False

            # Verify reservation is still in a payable state
            current_res = self.reservation_repo.get_by_id(reservation_id, conn=conn)
            if current_res is None:
                raise ReservationNotFoundError(f"Reservation '{reservation_id}' not found.")

            if current_res.status != ReservationStatus.RESERVED.value and current_res.status != ReservationStatus.PAYMENT_PENDING.value:
                existing_payment = self.payment_repo.get_by_reservation_id(reservation_id, conn=conn)
                if existing_payment is not None:
                    return existing_payment, False
                raise InvalidReservationStateError(
                    f"Reservation '{reservation_id}' is in state '{current_res.status}' and cannot be paid."
                )

            if result.status == PaymentGatewayStatus.SUCCESS:
                # SUCCESS: In ONE transaction:
                # - payment=SUCCESS
                # - reservation=CONFIRMED
                # - inventory reserved-1 / sold+1
                # - outbox_event ORDER_REQUESTED
                self.reservation_repo.update_status(reservation_id, ReservationStatus.CONFIRMED.value, conn=conn)
                self.inventory_repo.confirm_stock_sale(reservation.product_id, quantity=1, conn=conn)

                payment = Payment(
                    payment_id=payment_id,
                    reservation_id=reservation_id,
                    idempotency_key=idempotency_key,
                    status="SUCCESS",
                    gateway_ref=result.gateway_ref,
                )
                self.payment_repo.create(payment, conn=conn)

                payload = json.dumps({
                    "reservation_id": reservation_id,
                    "customer_id": reservation.customer_id,
                    "product_id": reservation.product_id,
                })
                outbox_event = OutboxEvent(
                    event_id=f"evt_{uuid.uuid4().hex[:12]}",
                    type="ORDER_REQUESTED",
                    payload=payload,
                    status="PENDING",
                )
                self.outbox_repo.create(outbox_event, conn=conn)
                return payment, True

            elif result.status == PaymentGatewayStatus.FAILURE:
                # FAILURE:
                # - payment=FAILED
                # - reservation=RELEASED
                # - inventory reserved-1 / available+1
                self.reservation_repo.update_status(reservation_id, ReservationStatus.RELEASED.value, conn=conn)
                self.inventory_repo.release_stock(reservation.product_id, quantity=1, conn=conn)

                payment = Payment(
                    payment_id=payment_id,
                    reservation_id=reservation_id,
                    idempotency_key=idempotency_key,
                    status="FAILED",
                    gateway_ref=None,
                )
                self.payment_repo.create(payment, conn=conn)
                return payment, True

            else:
                # TIMEOUT:
                # - mark payment=UNKNOWN
                # Stock stays reserved until reconciliation job checks gateway by idempotency_key
                payment = Payment(
                    payment_id=payment_id,
                    reservation_id=reservation_id,
                    idempotency_key=idempotency_key,
                    status="UNKNOWN",
                    gateway_ref=None,
                )
                self.payment_repo.create(payment, conn=conn)
                return payment, True
