"""Hackathon validation test suite verifying all core design assumptions:
1. Last-unit race condition (2 concurrent requests, 1 unit stock).
2. Idempotent duplicate request handling.
3. Payment failure releasing stock back to available pool.
4. Expiry worker returning stale reservations to available stock.
5. Order created after downstream outage recovery via outbox worker & circuit breaker.
6. Order state machine rejecting invalid state transitions.
"""

import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_connection
from app.models.inventory import Inventory
from app.models.outbox import OutboxEvent
from app.models.reservation import InventoryReservation, ReservationStatus
from app.models.order_state import (
    OrderStatus,
    get_order_state,
    InvalidStateTransitionError,
)
from app.repositories import (
    SqliteInventoryRepository,
    ReservationRepository,
    PaymentRepository,
    OrderRepository,
    OutboxRepository,
)
from app.services.payment_gateway import MockPaymentGateway
from app.services.payment_service import PaymentService
from app.services.circuit_breaker import CircuitBreaker
from app.services.order_service import MockOrderService
from app.workers.outbox_worker import OutboxWorker
from app.workers.expiry_worker import ExpiryWorker


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    """Sets up an isolated database for scenario tests."""
    db_file = str(tmp_path / "test_scenarios.db")
    monkeypatch.setenv("SALESTORM_DB_PATH", db_file)
    init_db(db_file)
    return db_file


def test_last_unit_race_two_concurrent_requests_one_unit(clean_db):
    """Assumption 1: Atomic update prevents overselling when 2 concurrent requests compete for 1 unit."""
    inv_repo = SqliteInventoryRepository(db_path=clean_db)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_last_unit",
            product_id="PRODUCT_X",
            total_stock=1,
            available_quantity=1,
            reserved_quantity=0,
            sold_quantity=0,
        )
    )

    client = TestClient(app)

    def reserve_call(idx: int):
        return client.post(
            "/reserve",
            json={"product_id": "PRODUCT_X", "customer_id": f"cust_race_{idx}"},
            headers={"Idempotency-Key": f"key_race_{idx}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(reserve_call, 1)
        f2 = executor.submit(reserve_call, 2)
        resp1, resp2 = f1.result(), f2.result()

    status_codes = sorted([resp1.status_code, resp2.status_code])
    # Exactly one succeeded (201 Created) and one rejected (409 Conflict)
    assert status_codes == [201, 409]

    # Verify inventory: available=0, reserved=1, sold=0, zero oversell
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 0
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True


def test_idempotent_duplicate_request(clean_db):
    """Assumption 2: Duplicate requests with identical Idempotency-Key return 200 without double-decrementing."""
    inv_repo = SqliteInventoryRepository(db_path=clean_db)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_idem",
            product_id="PRODUCT_X",
            total_stock=10,
            available_quantity=10,
            reserved_quantity=0,
            sold_quantity=0,
        )
    )

    client = TestClient(app)
    headers = {"Idempotency-Key": "same-client-key-123"}
    payload = {"product_id": "PRODUCT_X", "customer_id": "cust_idem"}

    # Initial request -> 201 Created
    r1 = client.post("/reserve", json=payload, headers=headers)
    assert r1.status_code == 201
    res_id = r1.json()["reservation_id"]

    # Duplicate replay -> 200 OK with same reservation_id
    r2 = client.post("/reserve", json=payload, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["reservation_id"] == res_id

    # Stock was decremented only ONCE (10 -> 9)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 9
    assert inv.reserved_quantity == 1
    assert inv.validate_conservation() is True


def test_payment_failure_releases_stock(clean_db):
    """Assumption 3: Payment gateway failure transitions reservation to RELEASED and returns stock to available."""
    inv_repo = SqliteInventoryRepository(db_path=clean_db)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_pay_fail",
            product_id="PRODUCT_X",
            total_stock=10,
            available_quantity=9,
            reserved_quantity=1,
            sold_quantity=0,
        )
    )

    res_repo = ReservationRepository(db_path=clean_db)
    res_repo.create(
        InventoryReservation(
            reservation_id="res_to_fail",
            product_id="PRODUCT_X",
            customer_id="cust_fail",
            idempotency_key="idemp_pay_fail_test",
            status=ReservationStatus.RESERVED.value,
            expires_at="2026-10-05 12:00:00",
        )
    )

    # Force 100% gateway failure
    mock_gateway = MockPaymentGateway(success_rate=0.0, failure_rate=1.0)
    service = PaymentService(gateway=mock_gateway, db_path=clean_db)

    payment, _ = service.process_payment(
        reservation_id="res_to_fail",
        idempotency_key="pay_fail_idem_key",
    )
    assert payment.status == "FAILED"

    # Reservation is RELEASED
    res = res_repo.get_by_id("res_to_fail")
    assert res.status == "RELEASED"

    # Stock returned: available 9 -> 10, reserved 1 -> 0
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 10
    assert inv.reserved_quantity == 0
    assert inv.validate_conservation() is True


def test_expiry_releases_stock(clean_db):
    """Assumption 4: Expiry worker finds stale reservations and returns unit to available stock."""
    inv_repo = SqliteInventoryRepository(db_path=clean_db)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_exp_test",
            product_id="PRODUCT_X",
            total_stock=5,
            available_quantity=4,
            reserved_quantity=1,
            sold_quantity=0,
        )
    )

    res_repo = ReservationRepository(db_path=clean_db)
    past_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    res_repo.create(
        InventoryReservation(
            reservation_id="res_expired_stale",
            product_id="PRODUCT_X",
            customer_id="cust_stale",
            idempotency_key="idemp_stale_key",
            status=ReservationStatus.RESERVED.value,
            expires_at=past_timestamp,
        )
    )

    worker = ExpiryWorker(db_path=clean_db)
    expired_count = worker.process_expired_batch()
    assert expired_count == 1

    # Reservation is EXPIRED
    res = res_repo.get_by_id("res_expired_stale")
    assert res.status == "EXPIRED"

    # Stock returned: available 4 -> 5, reserved 1 -> 0
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 5
    assert inv.reserved_quantity == 0
    assert inv.validate_conservation() is True


def test_order_created_after_outage(clean_db):
    """Assumption 5: OrderService downtime is absorbed by transactional outbox and recovered after restoration."""
    res_repo = ReservationRepository(db_path=clean_db)
    res_repo.create(
        InventoryReservation(
            reservation_id="res_outage_101",
            product_id="PRODUCT_X",
            customer_id="cust_outage_101",
            idempotency_key="idemp_outage_res",
            status=ReservationStatus.CONFIRMED.value,
            expires_at="2026-10-05 12:00:00",
        )
    )

    outbox_repo = OutboxRepository(db_path=clean_db)
    outbox_repo.create(
        OutboxEvent(
            event_id="evt_outage_1",
            type="ORDER_REQUESTED",
            payload=json.dumps({"reservation_id": "res_outage_101", "customer_id": "cust_outage_101"}),
            status="PENDING",
        )
    )

    # 1. Simulate OrderService DOWN
    mock_order_service = MockOrderService(db_path=clean_db)
    mock_order_service.set_down(True)
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=5.0)
    worker = OutboxWorker(order_service=mock_order_service, circuit_breaker=cb, db_path=clean_db)

    # Attempt during outage fails
    worker.process_batch()
    assert cb.failure_count == 1

    # 2. Restore OrderService
    mock_order_service.set_down(False)
    cb._on_success()

    # Worker retries and succeeds
    processed = worker.process_batch()
    assert processed == 1

    # Order exists in database
    ord_repo = OrderRepository(db_path=clean_db)
    order = ord_repo.get_by_reservation_id("res_outage_101")
    assert order is not None
    assert order.status == "CONFIRMED"

    # Outbox event marked PUBLISHED
    pending = outbox_repo.get_pending()
    assert len(pending) == 0


def test_invalid_state_transition_rejected():
    """Assumption 6: Order State pattern rejects illegal transitions while enforcing the legal lifecycle."""
    state = get_order_state(OrderStatus.CREATED)

    # Valid step 1: CREATED -> CONFIRMED
    state = state.transition_to(OrderStatus.CONFIRMED)
    assert state.status == OrderStatus.CONFIRMED

    # Invalid jump: Cannot jump directly from CONFIRMED to DELIVERED
    with pytest.raises(InvalidStateTransitionError):
        state.transition_to(OrderStatus.DELIVERED)

    # Valid step 2: CONFIRMED -> PROCESSING -> SHIPPED -> OUT_FOR_DELIVERY -> DELIVERED
    state = state.transition_to(OrderStatus.PROCESSING)
    state = state.transition_to(OrderStatus.SHIPPED)
    state = state.transition_to(OrderStatus.OUT_FOR_DELIVERY)
    state = state.transition_to(OrderStatus.DELIVERED)
    assert state.status == OrderStatus.DELIVERED

    # Terminal state rejection: DELIVERED cannot transition anywhere
    with pytest.raises(InvalidStateTransitionError):
        state.transition_to(OrderStatus.CREATED)


def test_200_concurrent_reserve_calls_same_idempotency_key(clean_db):
    """Requirement 5a: 200 concurrent /reserve calls with the SAME Idempotency-Key:
    exactly 1 reservation, stock decremented by exactly 1, all responses return the same reservation_id.
    """
    inv_repo = SqliteInventoryRepository(db_path=clean_db)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_same_key_test",
            product_id="PRODUCT_X",
            total_stock=100,
            available_quantity=100,
            reserved_quantity=0,
            sold_quantity=0,
        )
    )

    client = TestClient(app)
    idem_key = "idemp_same_key_200_unique"

    def make_reserve_call(idx):
        return client.post(
            "/reserve",
            json={"product_id": "PRODUCT_X", "customer_id": f"cust_{idx}"},
            headers={"Idempotency-Key": idem_key},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        responses = list(executor.map(make_reserve_call, range(200)))

    # Verify all responses returned 200 or 201
    for r in responses:
        assert r.status_code in (200, 201), f"Unexpected status: {r.status_code}, {r.text}"

    # Verify exactly one 201 Created was returned, remaining 199 are 200 OK
    created_count = sum(1 for r in responses if r.status_code == 201)
    replay_count = sum(1 for r in responses if r.status_code == 200)
    assert created_count == 1
    assert replay_count == 199

    # All responses returned the exact same reservation_id
    reservation_ids = {r.json()["reservation_id"] for r in responses}
    assert len(reservation_ids) == 1
    winning_res_id = reservation_ids.pop()

    # Verify database has exactly 1 reservation row
    res_repo = ReservationRepository(db_path=clean_db)
    res = res_repo.get_by_idempotency_key(idem_key)
    assert res is not None
    assert res.reservation_id == winning_res_id

    # Verify stock decremented by exactly 1
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 99
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0


def test_50_concurrent_pay_calls_same_idempotency_key(clean_db, monkeypatch):
    """Requirement 5b: 50 concurrent /pay calls with the SAME Idempotency-Key:
    exactly 1 payment row and exactly 1 gateway charge.
    """
    inv_repo = SqliteInventoryRepository(db_path=clean_db)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_same_pay_test",
            product_id="PRODUCT_X",
            total_stock=100,
            available_quantity=99,
            reserved_quantity=1,
            sold_quantity=0,
        )
    )

    res_repo = ReservationRepository(db_path=clean_db)
    reservation = InventoryReservation(
        reservation_id="res_same_pay_001",
        product_id="PRODUCT_X",
        customer_id="cust_pay_test",
        idempotency_key="res_idem_for_pay_test",
        status=ReservationStatus.RESERVED.value,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    res_repo.create(reservation)

    client = TestClient(app)
    pay_key = "idemp_same_pay_50_unique"

    # Use a custom gateway to strictly track charge calls
    gateway = MockPaymentGateway(success_rate=1.0, failure_rate=0.0)
    monkeypatch.setattr(
        "app.api.payment_router.PaymentService",
        lambda: PaymentService(gateway=gateway, db_path=clean_db),
    )

    def make_pay_call(idx):
        return client.post(
            "/pay",
            json={"reservation_id": "res_same_pay_001"},
            headers={"Idempotency-Key": pay_key},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        responses = list(executor.map(make_pay_call, range(50)))

    for r in responses:
        assert r.status_code in (200, 201), f"Unexpected status: {r.status_code}, {r.text}"
        assert r.json()["status"] == "SUCCESS"

    # Exactly 1 payment row in database
    pay_repo = PaymentRepository(db_path=clean_db)
    payment = pay_repo.get_by_idempotency_key(pay_key)
    assert payment is not None

    with get_connection(clean_db) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM payment WHERE idempotency_key = ?", (pay_key,))
        count = cur.fetchone()[0]
        assert count == 1

    # Exactly 1 charge recorded in gateway ledger
    assert len(gateway._ledger) == 1
    assert pay_key in gateway._ledger

    # Stock moved from reserved to sold
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 99
    assert inv.reserved_quantity == 0
    assert inv.sold_quantity == 1

