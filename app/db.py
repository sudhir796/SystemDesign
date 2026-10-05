"""Database configuration and connection management for SQLite in WAL mode."""

import os
import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional


def get_default_db_path() -> str:
    return os.getenv("SALESTORM_DB_PATH", "salestorm.db")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Creates and configures an SQLite connection with WAL mode and pragmas.
    
    WAL mode allows concurrent readers alongside a concurrent writer.
    isolation_level='IMMEDIATE' acquires write locks upfront, preventing upgrade deadlocks.
    busy_timeout ensures threads wait up to 30000ms instead of immediately throwing locked error.
    """
    path = db_path or get_default_db_path()
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False, isolation_level="IMMEDIATE")
    conn.row_factory = sqlite3.Row
    
    # Configure WAL mode and performance pragmas
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


@contextmanager
def get_db(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections, handling commit and rollback with BEGIN IMMEDIATE."""
    conn = get_connection(db_path)
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db(db_path: Optional[str] = None) -> None:
    """Initializes tables and indexes if they do not exist."""
    path = db_path or get_default_db_path()
    with get_db(path) as conn:
        cursor = conn.cursor()
        
        # 1. inventory table with stock conservation CHECK constraint
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            inventory_id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL UNIQUE,
            total_stock INTEGER NOT NULL,
            available_quantity INTEGER NOT NULL,
            reserved_quantity INTEGER NOT NULL DEFAULT 0,
            sold_quantity INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (available_quantity >= 0),
            CHECK (reserved_quantity >= 0),
            CHECK (sold_quantity >= 0),
            CHECK (available_quantity + reserved_quantity + sold_quantity = total_stock)
        );
        """)
        
        # 2. inventory_reservation table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory_reservation (
            reservation_id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (
                status IN ('RESERVED', 'PAYMENT_PENDING', 'CONFIRMED', 'RELEASED', 'EXPIRED')
            ),
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        # Indexes on inventory_reservation
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_reservation_status_expires 
        ON inventory_reservation(status, expires_at);
        """)
        
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_reservation_idempotency 
        ON inventory_reservation(idempotency_key);
        """)
        
        # 3. payment table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment (
            payment_id TEXT PRIMARY KEY,
            reservation_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            gateway_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (reservation_id) REFERENCES inventory_reservation(reservation_id)
        );
        """)
        
        # 4. orders table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            reservation_id TEXT NOT NULL UNIQUE,
            customer_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (reservation_id) REFERENCES inventory_reservation(reservation_id)
        );
        """)
        
        # 5. outbox_event table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS outbox_event (
            event_id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
