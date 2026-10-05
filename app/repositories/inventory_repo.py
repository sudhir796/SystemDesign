"""Inventory repository interface and SQLite implementation."""

from abc import ABC, abstractmethod
import sqlite3
from typing import Optional
from app.models.inventory import Inventory
from app.db import get_db, get_connection


class InventoryRepository(ABC):
    """Abstract interface for inventory data access, allowing interchangeable implementations."""

    @abstractmethod
    def get_by_product_id(
        self, product_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Inventory]:
        """Fetch inventory record by product_id."""
        pass

    @abstractmethod
    def reserve_stock(
        self, product_id: str, quantity: int = 1, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Atomically transition stock from available to reserved. Returns True if successful."""
        pass

    @abstractmethod
    def release_stock(
        self, product_id: str, quantity: int = 1, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Atomically transition stock from reserved back to available. Returns True if successful."""
        pass

    @abstractmethod
    def confirm_stock_sale(
        self, product_id: str, quantity: int = 1, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Atomically transition stock from reserved to sold. Returns True if successful."""
        pass

    @abstractmethod
    def upsert(
        self, inventory: Inventory, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        """Insert or update total stock and quantities."""
        pass


class SqliteInventoryRepository(InventoryRepository):
    """SQLite implementation of InventoryRepository operating in WAL mode."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def _get_active_conn(self, conn: Optional[sqlite3.Connection]):
        """Returns provided connection or creates a new context-managed one."""
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
        """Atomically decrement available and increment reserved.
        
        The WHERE available_quantity >= quantity prevents overselling.
        The table CHECK constraint ensures total conservation.
        """
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE inventory
                SET available_quantity = available_quantity - ?,
                    reserved_quantity = reserved_quantity + ?,
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ? AND available_quantity >= ?
                """,
                (quantity, quantity, product_id, quantity),
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
        """Atomically decrement reserved and increment available."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE inventory
                SET reserved_quantity = reserved_quantity - ?,
                    available_quantity = available_quantity + ?,
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ? AND reserved_quantity >= ?
                """,
                (quantity, quantity, product_id, quantity),
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
        """Atomically decrement reserved and increment sold."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE inventory
                SET reserved_quantity = reserved_quantity - ?,
                    sold_quantity = sold_quantity + ?,
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ? AND reserved_quantity >= ?
                """,
                (quantity, quantity, product_id, quantity),
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
        """Upsert inventory row."""
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
