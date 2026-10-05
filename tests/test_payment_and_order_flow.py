"""Tests for Phase 4: Payment gateway, outbox worker, circuit breaker, and order state machine."""

import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_connection
from app.models.inventory import Inventory
from app.models.outbox import OutboxEvent
from app.models.order_state import (
    OrderStatus,
    get_order_state,
    InvalidStateTransitionError,
)
from app.models.reservation import InventoryReservation, ReservationStatus
from app.repositories import (
    SqliteInventoryRepository,
    ReservationRepository,
    PaymentRepository,
    OrderRepository,
    OutboxRepository,
)
from app.services.payment_gateway import (
    MockPaymentGateway,
    PaymentGatewayStatus,
    PaymentGatewayResult,
)
from app.services.payment_service import PaymentService
from app.services.payment_reconciliation import PaymentReconciliationService
from app.services.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.services.order_service import MockOrderService, OrderServiceDownError
from app.workers.outbox_worker import OutboxWorker


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Sets up an isolated database with initial stock for testing."""
    db_file = str(tmp_path / "test_phase4.db")
    monkeypatch.setenv("SALESTORM_DB_PATH", db_file)
    init_db(db_file)

    # Seed 10 units, 2 reserved, 8 available
    inv_repo = SqliteInventoryRepository(db_path=db_file)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_p4",
            product_id="PRODUCT_X",
            total_stock=10,
            available_quantity=8,
            reserved_quantity=2,
            sold_quantity=0,
        )
    )

    # Create 2 initial reservations
    res_repo = ReservationRepository(db_path=db_file)
    res_repo.create(
        InventoryReservation(
            reservation_id="res_pay_success",
            product_id="PRODUCT_X",
            customer_id="cust_succ",
            idempotency_key="idemp_succ_res",
            status=ReservationStatus.RESERVED.value,
            expires_at="2026-10-05 12:00:00",
        )
    )
    res_repo.create(
        InventoryReservation(
            reservation_id="res_pay_fail",
            product_id="PRODUCT_X",
            customer_id="cust_fail",
            idempotency_key="idemp_fail_res",
            status=ReservationStatus.RESERVED.value,
            expires_at="2026-10-05 12:00:00",
        )
    )
    return db_file


def test_order_state_transitions():
    """Verify that legal transitions succeed and illegal jumps raise InvalidStateTransitionError."""
    state = get_order_state(OrderStatus.CREATED)
    assert state.status == OrderStatus.CREATED

    # Legal: CREATED -> CONFIRMED
    state = state.transition_to(OrderStatus.CONFIRMED)
    assert state.status == OrderStatus.CONFIRMED

    # Legal: CONFIRMED -> PROCESSING -> SHIPPED -> OUT_FOR_DELIVERY -> DELIVERED
    state = state.transition_to(OrderStatus.PROCESSING)
    assert state.status == OrderStatus.PROCESSING
    state = state.transition_to(OrderStatus.SHIPPED)
    assert state.status == OrderStatus.SHIPPED
    state = state.transition_to(OrderStatus.OUT_FOR_DELIVERY)
    assert state.status == OrderStatus.OUT_FOR_DELIVERY
    state = state.transition_to(OrderStatus.DELIVERED)
    assert state.status == OrderStatus.DELIVERED

    # Illegal: DELIVERED is terminal, cannot transition further
    with pytest.raises(InvalidStateTransitionError):
        state.transition_to(OrderStatus.PROCESSING)

    # Illegal: Jump from CREATED directly to SHIPPED
    created_state = get_order_state(OrderStatus.CREATED)
    with pytest.raises(InvalidStateTransitionError):
        created_state.transition_to(OrderStatus.SHIPPED)


def test_payment_endpoint_missing_header_returns_400(test_env):
    """Verify /pay returns 400 if Idempotency-Key header is omitted."""
    client = TestClient(app)
    resp = client.post("/pay", json={"reservation_id": "res_pay_success"})
    assert resp.status_code == 400
    assert "Missing Idempotency-Key" in resp.json()["detail"]


def test_payment_success_atomic_transaction(test_env):
    """Verify payment SUCCESS transitions stock, reservation, payment, and outbox atomically."""
    # Force 100% success on gateway
    mock_gateway = MockPaymentGateway(success_rate=1.0, failure_rate=0.0, timeout_rate=0.0)
    service = PaymentService(gateway=mock_gateway, db_path=test_env)

    payment, is_new = service.process_payment(
        reservation_id="res_pay_success",
        idempotency_key="pay_idemp_key_1",
    )
    assert is_new is True
    assert payment.status == "SUCCESS"
    assert payment.gateway_ref is not None

    # Verify reservation CONFIRMED
    res_repo = ReservationRepository(db_path=test_env)
    res = res_repo.get_by_id("res_pay_success")
    assert res.status == "CONFIRMED"

    # Verify inventory: reserved 2 -> 1, sold 0 -> 1, available remains 8
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 8
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 1
    assert inv.validate_conservation() is True

    # Verify outbox event ORDER_REQUESTED created
    outbox_repo = OutboxRepository(db_path=test_env)
    pending = outbox_repo.get_pending()
    assert len(pending) == 1
    assert pending[0].type == "ORDER_REQUESTED"
    payload = json.loads(pending[0].payload)
    assert payload["reservation_id"] == "res_pay_success"
    assert payload["customer_id"] == "cust_succ"


def test_payment_idempotency_replay(test_env):
    """Verify same idempotency key returns existing payment without double charging."""
    mock_gateway = MockPaymentGateway(success_rate=1.0, failure_rate=0.0, timeout_rate=0.0)
    service = PaymentService(gateway=mock_gateway, db_path=test_env)

    # First call
    p1, is_new1 = service.process_payment("res_pay_success", "idemp_same_key")
    assert is_new1 is True

    # Replay call
    p2, is_new2 = service.process_payment("res_pay_success", "idemp_same_key")
    assert is_new2 is False
    assert p1.payment_id == p2.payment_id

    # Inventory sold should only be 1
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.sold_quantity == 1
    assert inv.reserved_quantity == 1


def test_payment_failure_releases_stock(test_env):
    """Verify payment FAILURE releases stock from reserved back to available."""
    # Force 100% failure on gateway
    mock_gateway = MockPaymentGateway(success_rate=0.0, failure_rate=1.0, timeout_rate=0.0)
    service = PaymentService(gateway=mock_gateway, db_path=test_env)

    payment, is_new = service.process_payment(
        reservation_id="res_pay_fail",
        idempotency_key="pay_fail_key_1",
    )
    assert is_new is True
    assert payment.status == "FAILED"

    # Reservation must be RELEASED
    res_repo = ReservationRepository(db_path=test_env)
    res = res_repo.get_by_id("res_pay_fail")
    assert res.status == "RELEASED"

    # Stock returned: available 8 -> 9, reserved 2 -> 1, sold remains 0
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 9
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True


def test_payment_timeout_and_reconciliation(test_env):
    """Verify TIMEOUT marks payment UNKNOWN, and reconciliation job resolves it via gateway."""
    mock_gateway = MockPaymentGateway(success_rate=0.0, failure_rate=0.0, timeout_rate=1.0)
    # Configure deterministic reconciliation outcome as SUCCESS
    mock_gateway._reconciliation_outcomes["timeout_key_1"] = PaymentGatewayResult(
        status=PaymentGatewayStatus.SUCCESS, gateway_ref="gw_tx_reconciled_99"
    )

    service = PaymentService(gateway=mock_gateway, db_path=test_env)
    payment, is_new = service.process_payment("res_pay_success", "timeout_key_1")
    assert payment.status == "UNKNOWN"

    # Stock stays reserved pending reconciliation
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.reserved_quantity == 2

    # Run reconciliation
    reconciliation_service = PaymentReconciliationService(gateway=mock_gateway, db_path=test_env)
    resolved = reconciliation_service.reconcile_unknown_payments()
    assert resolved == 1

    # Payment now SUCCESS, stock confirmed
    pay_repo = PaymentRepository(db_path=test_env)
    updated_pay = pay_repo.get_by_id(payment.payment_id)
    assert updated_pay.status == "SUCCESS"
    assert updated_pay.gateway_ref == "gw_tx_reconciled_99"

    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 1


def test_outbox_worker_and_circuit_breaker(test_env):
    """Verify outbox worker creates orders, handles downtime, and trips circuit breaker."""
    # 1. Insert a pending outbox event
    outbox_repo = OutboxRepository(db_path=test_env)
    payload = json.dumps({"reservation_id": "res_pay_success", "customer_id": "cust_succ"})
    outbox_repo.create(
        OutboxEvent(
            event_id="evt_test_1",
            type="ORDER_REQUESTED",
            payload=payload,
            status="PENDING",
        )
    )

    # 2. Process with healthy OrderService
    mock_order_service = MockOrderService(db_path=test_env)
    worker = OutboxWorker(order_service=mock_order_service, db_path=test_env)
    processed = worker.process_batch()
    assert processed == 1

    # Order exists in DB
    ord_repo = OrderRepository(db_path=test_env)
    order = ord_repo.get_by_reservation_id("res_pay_success")
    assert order is not None
    assert order.status == "CONFIRMED"

    # Event is now PUBLISHED
    pending = outbox_repo.get_pending()
    assert len(pending) == 0

    # 3. Test Downtime and Circuit Breaker
    mock_order_service.set_down(True)  # Simulate OrderService down
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=10.0)
    failing_worker = OutboxWorker(
        order_service=mock_order_service,
        circuit_breaker=cb,
        max_attempts=2,
        db_path=test_env,
    )

    # Insert failing outbox event
    outbox_repo.create(
        OutboxEvent(
            event_id="evt_fail_1",
            type="ORDER_REQUESTED",
            payload=json.dumps({"reservation_id": "res_fail_2", "customer_id": "cust_fail_2"}),
            status="PENDING",
        )
    )

    # Attempt 1: Fails due to OrderService down
    failing_worker.process_batch()
    assert cb.failure_count == 1
    assert cb.state.value == "CLOSED"

    # Attempt 2: Fails, hits max_attempts (2) -> moves to DEAD_LETTER and trips circuit breaker
    failing_worker.process_batch()
    assert cb.failure_count == 2
    assert cb.state.value == "OPEN"

    # Verify event is in DEAD_LETTER after max_attempts
    conn = get_connection(test_env)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM outbox_event WHERE event_id = 'evt_fail_1'")
        status_in_db = cursor.fetchone()[0]
    finally:
        conn.close()
    assert status_in_db == "DEAD_LETTER"
