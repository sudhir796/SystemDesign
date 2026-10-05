"""Repository for inventory reservations."""

import sqlite3
from typing import List, Optional
from app.db import get_connection
from app.models.reservation import InventoryReservation


class ReservationRepository:
    """Handles CRUD operations for the inventory_reservation table."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def _get_active_conn(self, conn: Optional[sqlite3.Connection]):
        if conn is not None:
            return conn, False
        if self.db_path:
            return get_connection(self.db_path), True
        return get_connection(), True

    def create(
        self, reservation: InventoryReservation, conn: Optional[sqlite3.Connection] = None
    ) -> InventoryReservation:
        """Inserts a new reservation."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                INSERT INTO inventory_reservation (
                    reservation_id, product_id, customer_id,
                    idempotency_key, status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    reservation.reservation_id,
                    reservation.product_id,
                    reservation.customer_id,
                    reservation.idempotency_key,
                    reservation.status,
                    reservation.expires_at,
                ),
            )
            if is_owned:
                active_conn.commit()
            return reservation
        except Exception:
            if is_owned:
                active_conn.rollback()
            raise
        finally:
            if is_owned:
                active_conn.close()

    def get_by_id(
        self, reservation_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[InventoryReservation]:
        """Fetch reservation by primary key."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT reservation_id, product_id, customer_id,
                       idempotency_key, status, expires_at, created_at
                FROM inventory_reservation
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return InventoryReservation(
                reservation_id=row["reservation_id"],
                product_id=row["product_id"],
                customer_id=row["customer_id"],
                idempotency_key=row["idempotency_key"],
                status=row["status"],
                expires_at=row["expires_at"],
                created_at=row["created_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def get_by_idempotency_key(
        self, idempotency_key: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[InventoryReservation]:
        """Fetch reservation by unique idempotency key."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT reservation_id, product_id, customer_id,
                       idempotency_key, status, expires_at, created_at
                FROM inventory_reservation
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return InventoryReservation(
                reservation_id=row["reservation_id"],
                product_id=row["product_id"],
                customer_id=row["customer_id"],
                idempotency_key=row["idempotency_key"],
                status=row["status"],
                expires_at=row["expires_at"],
                created_at=row["created_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def update_status(
        self, reservation_id: str, status: str, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Update reservation status."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE inventory_reservation
                SET status = ?
                WHERE reservation_id = ?
                """,
                (status, reservation_id),
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

    def get_expired(
        self, now_iso: str, limit: int = 100, conn: Optional[sqlite3.Connection] = None
    ) -> List[InventoryReservation]:
        """Fetch active reservations that have passed their expires_at timestamp using the index."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT reservation_id, product_id, customer_id,
                       idempotency_key, status, expires_at, created_at
                FROM inventory_reservation
                WHERE status IN ('RESERVED', 'PAYMENT_PENDING') AND expires_at <= ?
                LIMIT ?
                """,
                (now_iso, limit),
            )
            rows = cursor.fetchall()
            return [
                InventoryReservation(
                    reservation_id=row["reservation_id"],
                    product_id=row["product_id"],
                    customer_id=row["customer_id"],
                    idempotency_key=row["idempotency_key"],
                    status=row["status"],
                    expires_at=row["expires_at"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        finally:
            if is_owned:
                active_conn.close()

    def conditionally_expire(
        self, reservation_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Conditionally updates status to EXPIRED only if currently RESERVED or PAYMENT_PENDING.
        
        Returns True if 1 row was updated, False if 0 rows (e.g. another worker already processed it).
        """
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE inventory_reservation
                SET status = 'EXPIRED'
                WHERE reservation_id = ? AND status IN ('RESERVED', 'PAYMENT_PENDING')
                """,
                (reservation_id,),
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

