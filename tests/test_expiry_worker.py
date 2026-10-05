"""Tests for Phase 5: Expiry Worker and concurrent worker safety."""

import concurrent.futures
from datetime import datetime, timedelta, timezone
import pytest

from app.config import Config
from app.db import init_db
from app.models.inventory import Inventory
from app.models.reservation import InventoryReservation, ReservationStatus
from app.repositories import (
    SqliteInventoryRepository,
    ReservationRepository,
)
from app.services.reservation_service import ReservationService
from app.workers.expiry_worker import ExpiryWorker


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Sets up an isolated database for expiry testing."""
    db_file = str(tmp_path / "test_expiry.db")
    monkeypatch.setenv("SALESTORM_DB_PATH", db_file)
    init_db(db_file)

    # Seed 5 total units: 3 available, 2 reserved, 0 sold
    inv_repo = SqliteInventoryRepository(db_path=db_file)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_exp",
            product_id="PRODUCT_X",
            total_stock=5,
            available_quantity=3,
            reserved_quantity=2,
            sold_quantity=0,
        )
    )
    return db_file


def test_expiry_worker_releases_stock(test_env):
    """Verify expired reservation transitions to EXPIRED and returns stock to available."""
    res_repo = ReservationRepository(db_path=test_env)
    
    # 1. Past timestamp (expired 10 seconds ago)
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
    res_repo.create(
        InventoryReservation(
            reservation_id="res_stale_1",
            product_id="PRODUCT_X",
            customer_id="cust_stale_1",
            idempotency_key="idemp_stale_1",
            status=ReservationStatus.RESERVED.value,
            expires_at=past_time,
        )
    )

    # 2. Future timestamp (active reservation, expires in 10 minutes)
    future_time = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    res_repo.create(
        InventoryReservation(
            reservation_id="res_active_1",
            product_id="PRODUCT_X",
            customer_id="cust_active_1",
            idempotency_key="idemp_active_1",
            status=ReservationStatus.RESERVED.value,
            expires_at=future_time,
        )
    )

    # Run ExpiryWorker
    worker = ExpiryWorker(db_path=test_env)
    expired_count = worker.process_expired_batch()
    assert expired_count == 1

    # Check stale reservation is EXPIRED
    stale_res = res_repo.get_by_id("res_stale_1")
    assert stale_res.status == "EXPIRED"

    # Check active reservation is STILL RESERVED
    active_res = res_repo.get_by_id("res_active_1")
    assert active_res.status == "RESERVED"

    # Check inventory returned: available 3 -> 4, reserved 2 -> 1
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 4
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True


def test_concurrent_expiry_workers_safety(test_env):
    """Verify that multiple expiry workers racing on the same expired reservation do not double-release stock.
    
    Uses conditional UPDATE ... WHERE status IN ('RESERVED', 'PAYMENT_PENDING')
    and checks affected row count.
    """
    res_repo = ReservationRepository(db_path=test_env)
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
    
    # Create 1 expired reservation
    res_repo.create(
        InventoryReservation(
            reservation_id="res_race_expire",
            product_id="PRODUCT_X",
            customer_id="cust_race_exp",
            idempotency_key="idemp_race_exp",
            status=ReservationStatus.RESERVED.value,
            expires_at=past_time,
        )
    )

    def run_worker_sweep(worker_id: int):
        worker = ExpiryWorker(db_path=test_env)
        return worker.process_expired_batch()

    # Launch 6 concurrent workers simultaneously
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(run_worker_sweep, i) for i in range(6)]
        results = [f.result() for f in futures]

    # Exactly ONE worker successfully expired the reservation
    assert sum(results) == 1

    # Stock only incremented once (available 3 -> 4, reserved 2 -> 1)
    inv_repo = SqliteInventoryRepository(db_path=test_env)
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    assert inv.available_quantity == 4
    assert inv.reserved_quantity == 1
    assert inv.sold_quantity == 0
    assert inv.validate_conservation() is True


def test_configurable_expiry_time(test_env, monkeypatch):
    """Verify that expiry time is configurable via RESERVATION_TTL_SECONDS (default 600, 5s for test)."""
    assert Config.reservation_ttl_seconds() == 600

    monkeypatch.setenv("RESERVATION_TTL_SECONDS", "5")
    assert Config.reservation_ttl_seconds() == 5

    service = ReservationService(db_path=test_env)
    res, is_new = service.reserve(
        product_id="PRODUCT_X",
        customer_id="cust_5s",
        idempotency_key="key_5s_test",
    )
    assert is_new is True

    # Parse expires_at and verify it's ~5 seconds in the future
    exp_time = datetime.strptime(res.expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    diff = (exp_time - datetime.now(timezone.utc)).total_seconds()
    assert 3 <= diff <= 7
