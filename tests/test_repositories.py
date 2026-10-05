"""Tests for database layer, schema integrity, and repository classes."""

import os
import sqlite3
import pytest

from app.db import init_db, get_connection
from app.models.inventory import Inventory
from app.models.reservation import InventoryReservation, ReservationStatus
from app.models.payment import Payment
from app.models.order import Order
from app.models.outbox import OutboxEvent
from app.repositories.inventory_repo import SqliteInventoryRepository
from app.repositories.reservation_repo import ReservationRepository
from app.repositories.payment_repo import PaymentRepository
from app.repositories.order_repo import OrderRepository
from app.repositories.outbox_repo import OutboxRepository


@pytest.fixture
def test_db(tmp_path):
    """Creates a temporary SQLite database initialized with our schema."""
    db_file = str(tmp_path / "test_salestorm.db")
    init_db(db_file)
    return db_file


def test_wal_mode_and_pragmas(test_db):
    """Verify that WAL mode and foreign key constraints are enabled."""
    conn = get_connection(test_db)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        assert mode.lower() == "wal"

        cursor.execute("PRAGMA foreign_keys;")
        fk = cursor.fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_inventory_check_constraints_stock_conservation(test_db):
    """Verify that inserting stock violating conservation raises IntegrityError."""
    conn = get_connection(test_db)
    try:
        cursor = conn.cursor()
        # available(50) + reserved(20) + sold(10) = 80 != total_stock(100) -> should fail
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO inventory (
                    inventory_id, product_id, total_stock, available_quantity,
                    reserved_quantity, sold_quantity
                ) VALUES ('inv_1', 'P1', 100, 50, 20, 10)
                """
            )
            conn.commit()

        # Negative quantity should fail
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO inventory (
                    inventory_id, product_id, total_stock, available_quantity,
                    reserved_quantity, sold_quantity
                ) VALUES ('inv_2', 'P2', 100, -1, 101, 0)
                """
            )
            conn.commit()
    finally:
        conn.close()


def test_inventory_repository_lifecycle(test_db):
    """Verify stock reservation, release, and confirmation lifecycle."""
    repo = SqliteInventoryRepository(db_path=test_db)

    # Seed 100 units
    initial = Inventory(
        inventory_id="inv_x",
        product_id="PRODUCT_X",
        total_stock=100,
        available_quantity=100,
        reserved_quantity=0,
        sold_quantity=0,
    )
    repo.upsert(initial)

    # 1. Reserve 1 unit
    success = repo.reserve_stock("PRODUCT_X", quantity=1)
    assert success is True
    inv = repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 99
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True

    # 2. Release 1 unit back to available
    success = repo.release_stock("PRODUCT_X", quantity=1)
    assert success is True
    inv = repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 100
    assert inv.reserved_quantity == 0

    # 3. Reserve 10 units and confirm sale
    assert repo.reserve_stock("PRODUCT_X", quantity=10) is True
    assert repo.confirm_stock_sale("PRODUCT_X", quantity=10) is True
    inv = repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 90
    assert inv.reserved_quantity == 0
    assert inv.sold_quantity == 10
    assert inv.validate_conservation() is True

    # 4. Cannot reserve more than available
    assert repo.reserve_stock("PRODUCT_X", quantity=95) is False
    inv = repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 90  # unchanged


def test_reservation_repository_and_idempotency(test_db):
    """Verify reservation creation, unique idempotency key, and status checks."""
    repo = ReservationRepository(db_path=test_db)

    res1 = InventoryReservation(
        reservation_id="res_001",
        product_id="PRODUCT_X",
        customer_id="cust_1",
        idempotency_key="idemp_1001",
        status=ReservationStatus.RESERVED.value,
        expires_at="2026-10-05T12:00:00Z",
    )
    repo.create(res1)

    fetched = repo.get_by_id("res_001")
    assert fetched is not None
    assert fetched.customer_id == "cust_1"
    assert fetched.status == "RESERVED"

    # Duplicate idempotency key must raise IntegrityError
    duplicate = InventoryReservation(
        reservation_id="res_002",
        product_id="PRODUCT_X",
        customer_id="cust_2",
        idempotency_key="idemp_1001",  # duplicate
        status=ReservationStatus.RESERVED.value,
        expires_at="2026-10-05T12:00:00Z",
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(duplicate)

    # Invalid status should violate CHECK constraint
    invalid_status_res = InventoryReservation(
        reservation_id="res_003",
        product_id="PRODUCT_X",
        customer_id="cust_3",
        idempotency_key="idemp_1003",
        status="INVALID_STATUS",
        expires_at="2026-10-05T12:00:00Z",
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(invalid_status_res)


def test_payment_and_order_repositories(test_db):
    """Verify payment and order insertion with foreign keys and uniqueness."""
    res_repo = ReservationRepository(db_path=test_db)
    pay_repo = PaymentRepository(db_path=test_db)
    ord_repo = OrderRepository(db_path=test_db)

    # Create parent reservation
    res = InventoryReservation(
        reservation_id="res_test_1",
        product_id="PRODUCT_X",
        customer_id="cust_test_1",
        idempotency_key="idemp_pay_1",
        status="RESERVED",
        expires_at="2026-10-05T12:00:00Z",
    )
    res_repo.create(res)

    # Create payment
    payment = Payment(
        payment_id="pay_001",
        reservation_id="res_test_1",
        idempotency_key="idemp_pay_unique_1",
        status="SUCCESS",
        gateway_ref="gw_tx_999",
    )
    pay_repo.create(payment)

    fetched_pay = pay_repo.get_by_id("pay_001")
    assert fetched_pay is not None
    assert fetched_pay.gateway_ref == "gw_tx_999"

    # Create order
    order = Order(
        order_id="ord_001",
        reservation_id="res_test_1",
        customer_id="cust_test_1",
        status="CONFIRMED",
    )
    ord_repo.create(order)

    fetched_ord = ord_repo.get_by_id("ord_001")
    assert fetched_ord is not None
    assert fetched_ord.customer_id == "cust_test_1"
    assert ord_repo.count_orders() == 1

    # Unique constraint on reservation_id prevents duplicate orders for same reservation
    duplicate_order = Order(
        order_id="ord_002",
        reservation_id="res_test_1",
        customer_id="cust_test_1",
        status="CONFIRMED",
    )
    with pytest.raises(sqlite3.IntegrityError):
        ord_repo.create(duplicate_order)


def test_outbox_repository(test_db):
    """Verify outbox event insertion, querying, and updating."""
    repo = OutboxRepository(db_path=test_db)

    event = OutboxEvent(
        event_id="evt_001",
        type="ORDER_CREATED",
        payload='{"order_id": "ord_001"}',
        status="PENDING",
    )
    repo.create(event)

    pending = repo.get_pending(limit=10)
    assert len(pending) == 1
    assert pending[0].event_id == "evt_001"

    repo.increment_attempts("evt_001")
    repo.update_status("evt_001", "PUBLISHED")

    pending_after = repo.get_pending(limit=10)
    assert len(pending_after) == 0
