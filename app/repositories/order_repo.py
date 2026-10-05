"""Repository for orders."""

import sqlite3
from typing import Optional
from app.db import get_connection
from app.models.order import Order


class OrderRepository:
    """Handles CRUD operations for the orders table."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def _get_active_conn(self, conn: Optional[sqlite3.Connection]):
        if conn is not None:
            return conn, False
        if self.db_path:
            return get_connection(self.db_path), True
        return get_connection(), True

    def create(self, order: Order, conn: Optional[sqlite3.Connection] = None) -> Order:
        """Inserts a new order.
        
        The reservation_id UNIQUE constraint prevents duplicate orders for the same reservation.
        """
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                INSERT INTO orders (order_id, reservation_id, customer_id, status, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (order.order_id, order.reservation_id, order.customer_id, order.status),
            )
            if is_owned:
                active_conn.commit()
            return order
        except Exception:
            if is_owned:
                active_conn.rollback()
            raise
        finally:
            if is_owned:
                active_conn.close()

    def get_by_id(
        self, order_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Order]:
        """Fetch order by order_id."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT order_id, reservation_id, customer_id, status, created_at
                FROM orders
                WHERE order_id = ?
                """,
                (order_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Order(
                order_id=row["order_id"],
                reservation_id=row["reservation_id"],
                customer_id=row["customer_id"],
                status=row["status"],
                created_at=row["created_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def get_by_reservation_id(
        self, reservation_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Order]:
        """Fetch order by associated reservation_id."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT order_id, reservation_id, customer_id, status, created_at
                FROM orders
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Order(
                order_id=row["order_id"],
                reservation_id=row["reservation_id"],
                customer_id=row["customer_id"],
                status=row["status"],
                created_at=row["created_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def count_orders(self, conn: Optional[sqlite3.Connection] = None) -> int:
        """Count total confirmed orders."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM orders")
            return cursor.fetchone()[0]
        finally:
            if is_owned:
                active_conn.close()
