"""Tests for the inventory reservation endpoint, idempotency, and concurrent race safety."""

import concurrent.futures
import sqlite3
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db
from app.models.inventory import Inventory
from app.repositories import (
    SqliteInventoryRepository,
    PessimisticInventoryRepository,
    ReservationRepository,
)
from app.services.reservation_service import ReservationService


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Sets up an isolated test database and overrides configuration."""
    db_file = str(tmp_path / "test_api.db")
    monkeypatch.setenv("SALESTORM_DB_PATH", db_file)
    init_db(db_file)
    
    # Seed 5 units for testing
    repo = SqliteInventoryRepository(db_path=db_file)
    repo.upsert(
        Inventory(
            inventory_id="inv_test",
            product_id="PRODUCT_X",
            total_stock=5,
            available_quantity=5,
            reserved_quantity=0,
            sold_quantity=0,
        )
    )
    return db_file


def test_missing_idempotency_key_returns_400(test_env):
    """Verify that omitting Idempotency-Key header returns HTTP 400 Bad Request."""
    client = TestClient(app)
    payload = {"customer_id": "cust_100", "product_id": "PRODUCT_X"}

    # No header provided
    resp_missing = client.post("/reserve", json=payload)
    assert resp_missing.status_code == 400
    assert "Missing Idempotency-Key" in resp_missing.json()["detail"]

    # Empty string header
    resp_empty = client.post("/reserve", json=payload, headers={"Idempotency-Key": "   "})
    assert resp_empty.status_code == 400
    assert "Missing Idempotency-Key" in resp_empty.json()["detail"]


def test_reserve_endpoint_success(test_env):
    """Verify standard reservation returns 201 with reservation_id and updates stock."""
    client = TestClient(app)
    
    headers = {"Idempotency-Key": "req_key_1"}
    payload = {"customer_id": "cust_101", "product_id": "PRODUCT_X"}
    
    response = client.post("/reserve", json=payload, headers=headers)
    assert response.status_code == 201
    data = response.json()
    assert "reservation_id" in data
    assert data["product_id"] == "PRODUCT_X"
    assert data["customer_id"] == "cust_101"
    assert data["status"] == "RESERVED"
    assert "expires_at" in data

    # Check database state
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 4
    assert inv.reserved_quantity == 1
    assert inv.validate_conservation() is True


def test_reserve_endpoint_idempotency_sequential(test_env):
    """Verify duplicate requests with the same Idempotency-Key return 200 and do not double-decrement."""
    client = TestClient(app)
    
    headers = {"Idempotency-Key": "req_key_duplicate"}
    payload = {"customer_id": "cust_102", "product_id": "PRODUCT_X"}
    
    # First call -> 201 Created
    res1 = client.post("/reserve", json=payload, headers=headers)
    assert res1.status_code == 201
    res1_id = res1.json()["reservation_id"]

    # Second call with same idempotency key -> Returns original reservation with 200 OK
    res2 = client.post("/reserve", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["reservation_id"] == res1_id

    # Third call with same idempotency key -> Still returns original reservation with 200 OK
    res3 = client.post("/reserve", json=payload, headers=headers)
    assert res3.status_code == 200
    assert res3.json()["reservation_id"] == res1_id

    # Stock should only be decremented once (5 -> 4)
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 4
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True


def test_identical_requests_race_condition_unique_constraint(tmp_path):
    """Verify the race condition where multiple identical requests arrive simultaneously.
    
    All requests share the same idempotency key.
    One request creates the reservation (201).
    Other requests hit UNIQUE constraint violation, roll back their transactions,
    and return the winning reservation (200).
    Total stock decrements exactly 1.
    """
    db_file = str(tmp_path / "test_race.db")
    init_db(db_file)
    
    inv_repo = SqliteInventoryRepository(db_path=db_file)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_race",
            product_id="PRODUCT_X",
            total_stock=10,
            available_quantity=10,
            reserved_quantity=0,
            sold_quantity=0,
        )
    )

    shared_idempotency_key = "shared_concurrent_key_999"

    def send_identical_request(worker_id: int):
        service = ReservationService(db_path=db_file)
        reservation, is_new = service.reserve(
            product_id="PRODUCT_X",
            customer_id=f"cust_race",
            idempotency_key=shared_idempotency_key,
        )
        return reservation.reservation_id, is_new

    # Launch 8 concurrent threads with the exact same idempotency key
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(send_identical_request, i) for i in range(8)]
        results = [f.result() for f in futures]

    reservation_ids = [r[0] for r in results]
    is_new_flags = [r[1] for r in results]

    # Exactly ONE thread created a new reservation
    assert is_new_flags.count(True) == 1
    # The other 7 threads received the existing record
    assert is_new_flags.count(False) == 7

    # All threads got the exact same reservation ID
    assert len(set(reservation_ids)) == 1

    # Total stock was decremented ONLY ONCE (10 -> 9)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 9
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True


def test_reserve_endpoint_sold_out_409(test_env):
    """Verify that when available quantity hits zero, 409 SOLD_OUT is returned."""
    client = TestClient(app)
    
    # Total stock is 5, reserve 5 units
    for i in range(5):
        resp = client.post(
            "/reserve",
            json={"customer_id": f"cust_{i}", "product_id": "PRODUCT_X"},
            headers={"Idempotency-Key": f"key_{i}"},
        )
        assert resp.status_code == 201

    # 6th attempt must return 409 SOLD_OUT
    resp_sold_out = client.post(
        "/reserve",
        json={"customer_id": "cust_overflow", "product_id": "PRODUCT_X"},
        headers={"Idempotency-Key": "key_overflow"},
    )
    assert resp_sold_out.status_code == 409
    assert resp_sold_out.json()["detail"] == "SOLD_OUT"

    # Invariants check
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 0
    assert inv.reserved_quantity == 5
    assert inv.validate_conservation() is True


def test_pessimistic_inventory_repository(test_env):
    """Verify PessimisticInventoryRepository functions correctly and respects stock bounds."""
    pessimistic_repo = PessimisticInventoryRepository(db_path=test_env)
    service = ReservationService(inventory_repo=pessimistic_repo, db_path=test_env)

    # We started with 5 units, let's reserve all 5
    for i in range(5):
        res, is_new = service.reserve(
            product_id="PRODUCT_X",
            customer_id=f"pess_cust_{i}",
            idempotency_key=f"pess_key_{i}",
        )
        assert res is not None
        assert is_new is True

    # 6th reservation should fail (None)
    res_overflow, _ = service.reserve(
        product_id="PRODUCT_X",
        customer_id="pess_cust_overflow",
        idempotency_key="pess_key_overflow",
    )
    assert res_overflow is None

    inv = pessimistic_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 0
    assert inv.reserved_quantity == 5
    assert inv.validate_conservation() is True


def test_concurrent_reservations_no_oversell(tmp_path):
    """Test concurrent requests reserving from a pool of 20 items.
    
    Verifies that under concurrent attempts, exactly 20 succeed and 10 fail with SOLD_OUT.
    """
    db_file = str(tmp_path / "test_concurrency.db")
    init_db(db_file)
    
    inv_repo = SqliteInventoryRepository(db_path=db_file)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_conc",
            product_id="PRODUCT_X",
            total_stock=20,
            available_quantity=20,
            reserved_quantity=0,
            sold_quantity=0,
        )
    )

    def attempt_reserve(i: int):
        service = ReservationService(db_path=db_file)
        res, is_new = service.reserve(
            product_id="PRODUCT_X",
            customer_id=f"concurrent_cust_{i}",
            idempotency_key=f"conc_key_{i}",
        )
        return res is not None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt_reserve, i) for i in range(30)]
        results = [f.result() for f in futures]

    successes = sum(1 for r in results if r is True)
    failures = sum(1 for r in results if r is False)

    assert successes == 20
    assert failures == 10

    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 0
    assert inv.reserved_quantity == 20
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True
