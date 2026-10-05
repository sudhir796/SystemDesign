"""Repository for payments."""

import sqlite3
from typing import Optional
from app.db import get_connection
from app.models.payment import Payment


class PaymentRepository:
    """Handles CRUD operations for the payment table."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def _get_active_conn(self, conn: Optional[sqlite3.Connection]):
        if conn is not None:
            return conn, False
        if self.db_path:
            return get_connection(self.db_path), True
        return get_connection(), True

    def create(
        self, payment: Payment, conn: Optional[sqlite3.Connection] = None
    ) -> Payment:
        """Inserts a new payment record."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                INSERT INTO payment (
                    payment_id, reservation_id, idempotency_key,
                    status, gateway_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    payment.payment_id,
                    payment.reservation_id,
                    payment.idempotency_key,
                    payment.status,
                    payment.gateway_ref,
                ),
            )
            if is_owned:
                active_conn.commit()
            return payment
        except Exception:
            if is_owned:
                active_conn.rollback()
            raise
        finally:
            if is_owned:
                active_conn.close()

    def get_by_id(
        self, payment_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Payment]:
        """Fetch payment by payment_id."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT payment_id, reservation_id, idempotency_key,
                       status, gateway_ref, created_at
                FROM payment
                WHERE payment_id = ?
                """,
                (payment_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Payment(
                payment_id=row["payment_id"],
                reservation_id=row["reservation_id"],
                idempotency_key=row["idempotency_key"],
                status=row["status"],
                gateway_ref=row["gateway_ref"],
                created_at=row["created_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def get_by_idempotency_key(
        self, idempotency_key: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Payment]:
        """Fetch payment by unique idempotency key."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT payment_id, reservation_id, idempotency_key,
                       status, gateway_ref, created_at
                FROM payment
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Payment(
                payment_id=row["payment_id"],
                reservation_id=row["reservation_id"],
                idempotency_key=row["idempotency_key"],
                status=row["status"],
                gateway_ref=row["gateway_ref"],
                created_at=row["created_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def get_by_reservation_id(
        self, reservation_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Payment]:
        """Fetch payment by reservation_id."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT payment_id, reservation_id, idempotency_key,
                       status, gateway_ref, created_at
                FROM payment
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Payment(
                payment_id=row["payment_id"],
                reservation_id=row["reservation_id"],
                idempotency_key=row["idempotency_key"],
                status=row["status"],
                gateway_ref=row["gateway_ref"],
                created_at=row["created_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def update_status(
        self,
        payment_id: str,
        status: str,
        gateway_ref: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> bool:
        """Update payment status and optional gateway reference."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE payment
                SET status = ?,
                    gateway_ref = COALESCE(?, gateway_ref)
                WHERE payment_id = ?
                """,
                (status, gateway_ref, payment_id),
            )
            updated = cursor.rowcount > 0
            if is_owned:
                active_conn.commit()
            return updated
        except Exception:
            if is_owned:
                active_conn.rollback()
            raise
        finally:
            if is_owned:
                active_conn.close()
