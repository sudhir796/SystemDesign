"""Pessimistic locking inventory repository implementation."""

import sqlite3
import threading
from typing import Optional
from app.db import get_connection
from app.models.inventory import Inventory
from app.repositories.inventory_repo import InventoryRepository


class PessimisticInventoryRepository(InventoryRepository):
    """Alternative InventoryRepository that simulates pessimistic locking (SELECT ... FOR UPDATE).
    
    Uses an explicit lock to serialize read-then-write operations on inventory,
    demonstrating lock contention differences against atomic updates in load tests.
    """

    # Global lock simulating database table/row-level pessimistic lock in SQLite
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def _get_active_conn(self, conn: Optional[sqlite3.Connection]):
        if conn is not None:
            return conn, False
        if self.db_path:
            return get_connection(self.db_path), True
        return get_connection(), True

    def get_by_product_id(
        self, product_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Inventory]:
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT inventory_id, product_id, total_stock, available_quantity,
                       reserved_quantity, sold_quantity, version, updated_at
                FROM inventory
                WHERE product_id = ?
                """,
                (product_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Inventory(
                inventory_id=row["inventory_id"],
                product_id=row["product_id"],
                total_stock=row["total_stock"],
                available_quantity=row["available_quantity"],
                reserved_quantity=row["reserved_quantity"],
                sold_quantity=row["sold_quantity"],
                version=row["version"],
                updated_at=row["updated_at"],
            )
        finally:
            if is_owned:
                active_conn.close()

    def reserve_stock(
        self, product_id: str, quantity: int = 1, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Pessimistic reserve: acquires lock, performs read-then-write."""
        with self._lock:
            active_conn, is_owned = self._get_active_conn(conn)
            try:
                cursor = active_conn.cursor()
                
                # Step 1: Pessimistic Read (SELECT ... FOR UPDATE equivalent)
                cursor.execute(
                    """
                    SELECT available_quantity FROM inventory WHERE product_id = ?
                    """,
                    (product_id,),
                )
                row = cursor.fetchone()
                if not row or row["available_quantity"] < quantity:
                    return False
                
                # Step 2: Write after read
                cursor.execute(
                    """
                    UPDATE inventory
                    SET available_quantity = available_quantity - ?,
                        reserved_quantity = reserved_quantity + ?,
                        version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE product_id = ?
                    """,
                    (quantity, quantity, product_id),
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

    def release_stock(
        self, product_id: str, quantity: int = 1, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Pessimistic release."""
        with self._lock:
            active_conn, is_owned = self._get_active_conn(conn)
            try:
                cursor = active_conn.cursor()
                cursor.execute(
                    "SELECT reserved_quantity FROM inventory WHERE product_id = ?",
                    (product_id,),
                )
                row = cursor.fetchone()
                if not row or row["reserved_quantity"] < quantity:
                    return False

                cursor.execute(
                    """
                    UPDATE inventory
                    SET reserved_quantity = reserved_quantity - ?,
                        available_quantity = available_quantity + ?,
                        version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE product_id = ?
                    """,
                    (quantity, quantity, product_id),
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

    def confirm_stock_sale(
        self, product_id: str, quantity: int = 1, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Pessimistic confirm sale."""
        with self._lock:
            active_conn, is_owned = self._get_active_conn(conn)
            try:
                cursor = active_conn.cursor()
                cursor.execute(
                    "SELECT reserved_quantity FROM inventory WHERE product_id = ?",
                    (product_id,),
                )
                row = cursor.fetchone()
                if not row or row["reserved_quantity"] < quantity:
                    return False

                cursor.execute(
                    """
                    UPDATE inventory
                    SET reserved_quantity = reserved_quantity - ?,
                        sold_quantity = sold_quantity + ?,
                        version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE product_id = ?
                    """,
                    (quantity, quantity, product_id),
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

    def upsert(
        self, inventory: Inventory, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        with self._lock:
            active_conn, is_owned = self._get_active_conn(conn)
            try:
                cursor = active_conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO inventory (
                        inventory_id, product_id, total_stock, available_quantity,
                        reserved_quantity, sold_quantity, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(product_id) DO UPDATE SET
                        total_stock = excluded.total_stock,
                        available_quantity = excluded.available_quantity,
                        reserved_quantity = excluded.reserved_quantity,
                        sold_quantity = excluded.sold_quantity,
                        version = inventory.version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        inventory.inventory_id,
                        inventory.product_id,
                        inventory.total_stock,
                        inventory.available_quantity,
                        inventory.reserved_quantity,
                        inventory.sold_quantity,
                        inventory.version,
                    ),
                )
                if is_owned:
                    active_conn.commit()
            except Exception:
                if is_owned:
                    active_conn.rollback()
                raise
            finally:
                if is_owned:
                    active_conn.close()
