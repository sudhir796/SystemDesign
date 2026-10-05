"""Payment reconciliation job for resolving UNKNOWN / timed out payments."""

import json
from typing import Optional
import uuid

from app.config import Config
from app.db import get_connection, get_db
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
    PaymentGatewayStatus,
)


class PaymentReconciliationService:
    """Reconciles UNKNOWN payments by polling the payment gateway using the idempotency key."""

    def __init__(
        self,
        gateway: PaymentGateway,
        inventory_repo: Optional[InventoryRepository] = None,
        reservation_repo: Optional[ReservationRepository] = None,
        payment_repo: Optional[PaymentRepository] = None,
        outbox_repo: Optional[OutboxRepository] = None,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path or Config.db_path()
        self.gateway = gateway
        self.inventory_repo = inventory_repo or get_inventory_repository(db_path=self.db_path)
        self.reservation_repo = reservation_repo or ReservationRepository(db_path=self.db_path)
        self.payment_repo = payment_repo or PaymentRepository(db_path=self.db_path)
        self.outbox_repo = outbox_repo or OutboxRepository(db_path=self.db_path)

    def reconcile_unknown_payments(self) -> int:
        """Finds all UNKNOWN payments, queries gateway status, and atomically resolves them."""
        # Find UNKNOWN payments
        conn = get_connection(self.db_path)
        unknown_payments = []
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT payment_id, reservation_id, idempotency_key, status, gateway_ref, created_at
                FROM payment
                WHERE status = 'UNKNOWN'
                """
            )
            rows = cursor.fetchall()
            for r in rows:
                unknown_payments.append(
                    Payment(
                        payment_id=r["payment_id"],
                        reservation_id=r["reservation_id"],
                        idempotency_key=r["idempotency_key"],
                        status=r["status"],
                        gateway_ref=r["gateway_ref"],
                        created_at=r["created_at"],
                    )
                )
        finally:
            conn.close()

        resolved_count = 0
        for payment in unknown_payments:
            gateway_res = self.gateway.check_status(payment.idempotency_key)
            if not gateway_res or gateway_res.status == PaymentGatewayStatus.TIMEOUT:
                # Still unknown, check on next pass
                continue

            reservation = self.reservation_repo.get_by_id(payment.reservation_id)
            if not reservation:
                continue

            with get_db(self.db_path) as active_conn:
                if gateway_res.status == PaymentGatewayStatus.SUCCESS:
                    # Gateway succeeded: confirm reservation and stock
                    self.reservation_repo.update_status(
                        reservation.reservation_id, ReservationStatus.CONFIRMED.value, conn=active_conn
                    )
                    self.inventory_repo.confirm_stock_sale(
                        reservation.product_id, quantity=1, conn=active_conn
                    )
                    self.payment_repo.update_status(
                        payment.payment_id, "SUCCESS", gateway_ref=gateway_res.gateway_ref, conn=active_conn
                    )
                    
                    payload = json.dumps({
                        "reservation_id": reservation.reservation_id,
                        "customer_id": reservation.customer_id,
                        "product_id": reservation.product_id,
                    })
                    self.outbox_repo.create(
                        OutboxEvent(
                            event_id=f"evt_{uuid.uuid4().hex[:12]}",
                            type="ORDER_REQUESTED",
                            payload=payload,
                            status="PENDING",
                        ),
                        conn=active_conn,
                    )
                    resolved_count += 1

                elif gateway_res.status == PaymentGatewayStatus.FAILURE:
                    # Gateway failed: release reservation and stock
                    self.reservation_repo.update_status(
                        reservation.reservation_id, ReservationStatus.RELEASED.value, conn=active_conn
                    )
                    self.inventory_repo.release_stock(
                        reservation.product_id, quantity=1, conn=active_conn
                    )
                    self.payment_repo.update_status(
                        payment.payment_id, "FAILED", conn=active_conn
                    )
                    resolved_count += 1

        return resolved_count
