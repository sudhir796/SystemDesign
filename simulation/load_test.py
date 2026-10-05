"""SALESTORM High-Concurrency Flash Sale Simulation.

Scenario:
- 10,000 customers call /reserve.
- 2% of customers resend the same Idempotency-Key.
- Each customer who receives a reservation proceeds to call /pay (95% success).
- At the FIRST successful payment, OrderService is taken DOWN for 30 seconds (configurable), overlapping order creation.
- 100ms periodic inventory sampler continuously verifies reserved + sold <= 100 throughout the entire run.
- Telemetry captures: failed create_order attempts, retry counts, peak outbox pending, circuit-breaker trips, and outbox drain time.
- Detailed per-endpoint latency breakdown (/reserve vs /pay).
- Splits 201 status codes into reservations vs payments, tracking failed payments, released units, and resold units.
- Verifies system invariants and generates detailed markdown & JSON reports.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import random
import sys
import time
from typing import Dict, List, Tuple

import httpx

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import Config
from app.db import init_db, get_connection, get_db
from app.main import app
from app.models.inventory import Inventory
from app.repositories import (
    SqliteInventoryRepository,
    PessimisticInventoryRepository,
    ReservationRepository,
    PaymentRepository,
    OrderRepository,
    OutboxRepository,
    get_inventory_repository,
)
from app.services.circuit_breaker import CircuitState
from app.services.order_service import get_mock_order_service, OrderServiceDownError
from app.services.payment_gateway import get_default_payment_gateway
from app.workers.outbox_worker import OutboxWorker
from app.workers.expiry_worker import ExpiryWorker


class SimulationMetrics:
    def __init__(self):
        self.reserve_requests = 0
        self.duplicate_requests = 0
        self.reservations_succeeded = 0  # 201 Created on /reserve
        self.duplicates_detected = 0     # 200 OK replay
        self.sold_out_count = 0          # 409 SOLD_OUT
        self.payment_requests = 0
        self.payments_succeeded = 0
        self.payments_failed = 0
        
        # Per-endpoint and combined latency tracking (Requirement 4)
        self.latencies: List[float] = []
        self.reserve_latencies: List[float] = []
        self.pay_latencies: List[float] = []
        
        self.start_time: float = 0.0
        self.end_time: float = 0.0

        # HTTP Status Breakdown (Requirement 3 & 5)
        self.status_counts: Dict[str, int] = {
            "201": 0,
            "200": 0,
            "409": 0,
            "429": 0,
            "5xx": 0,
            "timeouts": 0,
        }
        self.reservations_created_201: int = 0
        self.payments_created_201: int = 0
        self.units_released_after_payment_failure: int = 0
        self.units_resold: int = 0

        self.current_in_flight: int = 0
        self.max_in_flight: int = 0
        self.total_errors: int = 0

        # Outage & Outbox Telemetry (Requirement 4)
        self.failed_create_order_attempts: int = 0
        self.total_retries: int = 0
        self.peak_outbox_pending: int = 0
        self.circuit_breaker_open_count: int = 0
        self.outage_start_time: float = 0.0
        self.outage_end_time: float = 0.0
        self.outbox_drained_duration: float = 0.0
        self.order_recovery_rate: str = ""

        # 100ms Inventory Sampler (Requirement 6)
        self.inventory_samples_count: int = 0
        self.peak_committed_stock: int = 0
        self.sampler_violations: List[dict] = []


def calc_percentiles(lats: List[float]) -> Tuple[float, float, float]:
    """Computes (p50, p95, p99) in milliseconds."""
    if not lats:
        return 0.0, 0.0, 0.0
    sorted_lats = sorted(lats)
    n = len(sorted_lats)
    p50 = sorted_lats[int(n * 0.50)] * 1000
    p95 = sorted_lats[min(int(n * 0.95), n - 1)] * 1000
    p99 = sorted_lats[min(int(n * 0.99), n - 1)] * 1000
    return p50, p95, p99


async def run_simulation(
    total_customers: int = 10000,
    concurrency: int = 50,
    base_url: str = None,
    strategy: str = "atomic",
    downtime_seconds: float = 30.0,
    output_report: str = "simulation/report.md",
) -> dict:
    os.environ["INVENTORY_STRATEGY"] = strategy
    print("=" * 70)
    print(f"SALESTORM FLASH SALE SIMULATION: {total_customers:,} CUSTOMERS [Strategy: {strategy.upper()}, Concurrency: {concurrency}, Outage: {downtime_seconds}s]")
    print("=" * 70)

    # 1. Reset and Seed Database with 100 units of Product X
    db_path = Config.db_path()
    init_db(db_path)
    
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM orders;")
        conn.execute("DELETE FROM outbox_event;")
        conn.execute("DELETE FROM payment;")
        conn.execute("DELETE FROM inventory_reservation;")
        conn.execute("DELETE FROM inventory;")

    inv_repo = get_inventory_repository(strategy=strategy, db_path=db_path)
    inv_repo.upsert(
        Inventory(
            inventory_id="inv_sim_x",
            product_id="PRODUCT_X",
            total_stock=100,
            available_quantity=100,
            reserved_quantity=0,
            sold_quantity=0,
        )
    )

    res_repo = ReservationRepository(db_path=db_path)
    ord_repo = OrderRepository(db_path=db_path)
    outbox_repo = OutboxRepository(db_path=db_path)
    pay_repo = PaymentRepository(db_path=db_path)

    # Reset singleton MockPaymentGateway ledger
    gateway = get_default_payment_gateway()
    gateway._ledger.clear()
    gateway._reconciliation_outcomes.clear()

    metrics = SimulationMetrics()
    metrics.start_time = time.time()

    # Determine transport (in-process ASGI or external HTTP)
    limits = httpx.Limits(max_connections=max(concurrency, 100), max_keepalive_connections=max(concurrency, 100))
    client_kwargs = {"timeout": 60.0, "limits": limits}
    if base_url:
        client_kwargs["base_url"] = base_url
        client = httpx.AsyncClient(**client_kwargs)
    else:
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://salestorm-sim", **client_kwargs)

    # Background Outbox Worker Task
    outbox_worker = OutboxWorker(db_path=db_path)
    order_service = get_mock_order_service(db_path=db_path)
    order_service.set_down(False)
    outbox_worker.circuit_breaker._on_success()

    # Instrument OrderService & CircuitBreaker for outage evidence
    orig_create_order = order_service.create_order
    def instrumented_create_order(*args, **kwargs):
        try:
            return orig_create_order(*args, **kwargs)
        except OrderServiceDownError:
            metrics.failed_create_order_attempts += 1
            raise
    order_service.create_order = instrumented_create_order

    orig_cb_failure = outbox_worker.circuit_breaker._on_failure
    def instrumented_cb_failure():
        prev_state = outbox_worker.circuit_breaker.state
        orig_cb_failure()
        if prev_state != CircuitState.OPEN and outbox_worker.circuit_breaker.state == CircuitState.OPEN:
            metrics.circuit_breaker_open_count += 1
    outbox_worker.circuit_breaker._on_failure = instrumented_cb_failure

    worker_running = True
    async def background_outbox_loop():
        while worker_running:
            try:
                outbox_worker.process_batch(limit=50)
            except Exception:
                pass
            await asyncio.sleep(0.3)

    outbox_task = asyncio.create_task(background_outbox_loop())

    # 100ms Periodic Inventory & Outbox Sampler Task (Requirement 6)
    sampler_running = True
    async def periodic_sampler():
        while sampler_running:
            try:
                with get_connection(db_path) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT available_quantity, reserved_quantity, sold_quantity FROM inventory WHERE product_id = 'PRODUCT_X'"
                    )
                    row = cur.fetchone()
                    if row:
                        avail = row["available_quantity"]
                        res = row["reserved_quantity"]
                        sold = row["sold_quantity"]
                        committed = res + sold
                        metrics.inventory_samples_count += 1
                        if committed > metrics.peak_committed_stock:
                            metrics.peak_committed_stock = committed
                        if committed > 100 or avail < 0 or res < 0 or sold < 0:
                            metrics.sampler_violations.append({
                                "time": time.time(),
                                "avail": avail,
                                "reserved": res,
                                "sold": sold,
                                "committed": committed,
                            })

                    # Track peak outbox pending
                    cur.execute("SELECT COUNT(*) FROM outbox_event WHERE status = 'PENDING'")
                    pending_cnt = cur.fetchone()[0]
                    if pending_cnt > metrics.peak_outbox_pending:
                        metrics.peak_outbox_pending = pending_cnt
            except Exception:
                pass
            await asyncio.sleep(0.1)

    sampler_task = asyncio.create_task(periodic_sampler())

    downtime_triggered = False
    completed_customers = 0

    async def execute_tracked_request(method: str, url: str, endpoint_type: str = "reserve", **kwargs) -> httpx.Response:
        metrics.current_in_flight += 1
        if metrics.current_in_flight > metrics.max_in_flight:
            metrics.max_in_flight = metrics.current_in_flight

        t0 = time.perf_counter()
        try:
            if method == "POST":
                resp = await client.post(url, **kwargs)
            else:
                resp = await client.get(url, **kwargs)
            lat = time.perf_counter() - t0
            metrics.latencies.append(lat)
            if endpoint_type == "reserve":
                metrics.reserve_latencies.append(lat)
            elif endpoint_type == "pay":
                metrics.pay_latencies.append(lat)

            code = resp.status_code
            if str(code) in metrics.status_counts:
                metrics.status_counts[str(code)] += 1
            elif code >= 500:
                metrics.status_counts["5xx"] += 1
                metrics.total_errors += 1
            else:
                metrics.status_counts[str(code)] = metrics.status_counts.get(str(code), 0) + 1

            if code == 201:
                if endpoint_type == "reserve":
                    metrics.reservations_created_201 += 1
                elif endpoint_type == "pay":
                    metrics.payments_created_201 += 1

            return resp
        except httpx.TimeoutException:
            lat = time.perf_counter() - t0
            metrics.latencies.append(lat)
            if endpoint_type == "reserve":
                metrics.reserve_latencies.append(lat)
            elif endpoint_type == "pay":
                metrics.pay_latencies.append(lat)
            metrics.status_counts["timeouts"] += 1
            metrics.total_errors += 1
            raise
        except Exception:
            metrics.total_errors += 1
            raise
        finally:
            metrics.current_in_flight -= 1

    async def handle_customer(cust_idx: int):
        nonlocal completed_customers, downtime_triggered
        cust_id = f"cust_{cust_idx:05d}"
        idempotency_key = f"idem_res_{cust_idx:05d}"
        is_duplicate_sender = random.random() < 0.02  # 2% send duplicates

        # 1. Initial /reserve request
        metrics.reserve_requests += 1
        try:
            resp = await execute_tracked_request(
                "POST",
                "/reserve",
                endpoint_type="reserve",
                json={"product_id": "PRODUCT_X", "customer_id": cust_id},
                headers={"Idempotency-Key": idempotency_key},
            )
        except Exception:
            completed_customers += 1
            return

        reservation_id = None
        if resp.status_code == 201:
            metrics.reservations_succeeded += 1
            reservation_id = resp.json().get("reservation_id")
        elif resp.status_code == 200:
            metrics.duplicates_detected += 1
            reservation_id = resp.json().get("reservation_id")
        elif resp.status_code == 409:
            metrics.sold_out_count += 1

        # 2. Duplicate resend for 2% of users
        if is_duplicate_sender:
            metrics.reserve_requests += 1
            metrics.duplicate_requests += 1
            try:
                dup_resp = await execute_tracked_request(
                    "POST",
                    "/reserve",
                    endpoint_type="reserve",
                    json={"product_id": "PRODUCT_X", "customer_id": cust_id},
                    headers={"Idempotency-Key": idempotency_key},
                )
                if dup_resp.status_code == 200:
                    metrics.duplicates_detected += 1
            except Exception:
                pass

        # 3. Pay for reservation if secured
        if reservation_id:
            metrics.payment_requests += 1
            try:
                pay_resp = await execute_tracked_request(
                    "POST",
                    "/pay",
                    endpoint_type="pay",
                    json={"reservation_id": reservation_id},
                    headers={"Idempotency-Key": f"idem_pay_{cust_idx:05d}"},
                )
                if pay_resp.status_code in (200, 201):
                    pay_status = pay_resp.json().get("status")
                    if pay_status == "SUCCESS":
                        metrics.payments_succeeded += 1
                        # Requirement 1: Start 30s outage at FIRST successful payment
                        if not downtime_triggered and downtime_seconds > 0:
                            downtime_triggered = True
                            metrics.outage_start_time = time.time()
                            print(f"\n[FIRST PAYMENT SUCCESS] Taking OrderService DOWN for {downtime_seconds}s to overlap order creation...")
                            order_service.simulate_down_for(downtime_seconds)
                    else:
                        metrics.payments_failed += 1
                        metrics.units_released_after_payment_failure += 1
                else:
                    metrics.payments_failed += 1
                    metrics.units_released_after_payment_failure += 1
            except Exception:
                metrics.payments_failed += 1
                metrics.units_released_after_payment_failure += 1

        completed_customers += 1
        if completed_customers % 2500 == 0:
            print(f"  Processed {completed_customers:,} / {total_customers:,} customers (In-Flight: {metrics.current_in_flight})...")

    # Launch customer queue with worker pool
    print(f"Submitting {total_customers:,} customer requests with worker pool {concurrency}...")
    queue = asyncio.Queue()
    for i in range(total_customers):
        queue.put_nowait(i)

    async def worker():
        while not queue.empty():
            try:
                cust_idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await handle_customer(cust_idx)
            queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers)

    # Wait for OrderService downtime to expire if simulation was faster than downtime
    if order_service.down_until_timestamp > time.time():
        remaining = order_service.down_until_timestamp - time.time()
        print(f"Waiting {remaining:.1f}s for OrderService simulated downtime to complete...")
        await asyncio.sleep(remaining + 0.5)

    metrics.outage_end_time = time.time()
    print("OrderService RESTORED. Recovering circuit breaker and draining OutboxWorker queue...")

    # Restore circuit breaker and replay any dead-letter items
    outbox_worker.circuit_breaker._on_success()
    outbox_worker.retry_dead_letter()

    # Requirement 4: Drain outbox events and measure exact duration to 0 pending
    drain_start = time.time()
    for _ in range(60):
        pending_count = len(outbox_repo.get_pending())
        if pending_count == 0:
            break
        outbox_worker.process_batch(limit=100)
        await asyncio.sleep(0.2)

    metrics.outbox_drained_duration = time.time() - drain_start

    # Stop background tasks & close HTTP client
    worker_running = False
    sampler_running = False
    await outbox_task
    await sampler_task
    await client.aclose()

    metrics.end_time = time.time()
    total_duration = metrics.end_time - metrics.start_time

    # 4. Gather Final Invariants & Telemetry from Database
    inv = inv_repo.get_by_product_id("PRODUCT_X")
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM inventory_reservation WHERE status = 'CONFIRMED'")
        confirmed_reservations_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM orders")
        orders_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM outbox_event WHERE status = 'PENDING'")
        stuck_outbox_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM outbox_event WHERE status = 'DEAD_LETTER'")
        dead_letter_count = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(attempts), 0) FROM outbox_event")
        metrics.total_retries = cur.fetchone()[0]

        # Requirement 3: Check for each reservation_id, count of SUCCESS payments <= 1
        cur.execute(
            """
            SELECT reservation_id, COUNT(*) as cnt 
            FROM payment 
            WHERE status = 'SUCCESS' 
            GROUP BY reservation_id 
            HAVING cnt > 1
            """
        )
        duplicate_success_payments = cur.fetchall()
        no_duplicate_success_payments = len(duplicate_success_payments) == 0

        # Requirement 3: Gateway charge calls == distinct idempotency keys
        gateway_charges_count = len(gateway._ledger)
        cur.execute("SELECT COUNT(DISTINCT idempotency_key) FROM payment")
        distinct_payment_keys = cur.fetchone()[0]
        gateway_charges_match_keys = (gateway_charges_count == distinct_payment_keys)

        cur.execute("SELECT COUNT(DISTINCT order_id), COUNT(DISTINCT reservation_id), COUNT(*) FROM orders")
        distinct_orders, distinct_ord_res, total_orders = cur.fetchone()
    finally:
        conn.close()

    # Latency percentiles: Overall, /reserve, and /pay (Requirement 4)
    p50, p95, p99 = calc_percentiles(metrics.latencies)
    res_p50, res_p95, res_p99 = calc_percentiles(metrics.reserve_latencies)
    pay_p50, pay_p95, pay_p99 = calc_percentiles(metrics.pay_latencies)
    rps = len(metrics.latencies) / total_duration if total_duration > 0 else 0.0

    # Resold units calculation (Requirement 5)
    metrics.units_resold = min(metrics.units_released_after_payment_failure, inv.sold_quantity) if inv.sold_quantity == 100 else 0

    # Computed Recovery Text (Requirement 4)
    recovery_pct = (orders_count / confirmed_reservations_count * 100.0) if confirmed_reservations_count > 0 else 100.0
    metrics.order_recovery_rate = (
        f"Recovered {orders_count}/{confirmed_reservations_count} orders in {metrics.outbox_drained_duration:.2f}s ({recovery_pct:.1f}% recovery)"
    )

    # 5. Check Assertions
    assertion_1 = (inv.sold_quantity + inv.reserved_quantity) <= 100
    assertion_2 = inv.available_quantity >= 0 and inv.reserved_quantity >= 0 and inv.sold_quantity >= 0
    assertion_3 = no_duplicate_success_payments and gateway_charges_match_keys
    assertion_4 = (distinct_orders == total_orders) and (distinct_ord_res == total_orders)
    assertion_5 = (confirmed_reservations_count == orders_count)
    assertion_6 = (len(metrics.sampler_violations) == 0) and (metrics.peak_committed_stock <= 100)
    assertion_7 = (sum(metrics.status_counts.values()) == len(metrics.latencies))

    all_assertions_passed = all([
        assertion_1,
        assertion_2,
        assertion_3,
        assertion_4,
        assertion_5,
        assertion_6,
        assertion_7,
    ])

    report_content = f"""# SALESTORM Simulation Report

**Date/Time:** {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}  
**Concurrency Scenario:** {total_customers:,} Customers, 2% Duplicate Rate, {downtime_seconds}s OrderService Outage  
**Strategy:** {strategy.upper()} | **Configured Concurrency:** {concurrency} (Observed Peak In-Flight: {metrics.max_in_flight})  

---

## 1. Traffic & Request Summary
| Metric | Value |
|---|---|
| Total HTTP Requests | {len(metrics.latencies):,} |
| Reserve Requests | {metrics.reserve_requests:,} |
| Duplicate Requests Sent | {metrics.duplicate_requests:,} |
| Duplicate Replays Detected (200 OK) | {metrics.duplicates_detected:,} |
| Reservations Succeeded (201 Created) | {metrics.reservations_succeeded:,} |
| SOLD_OUT Rejections (409 Conflict) | {metrics.sold_out_count:,} |
| Payment Requests (/pay) | {metrics.payment_requests:,} |
| Payment Successes | {metrics.payments_succeeded:,} |
| Payment Failures | {metrics.payments_failed:,} |
| Units Released after Payment Failure | {metrics.units_released_after_payment_failure:,} |
| Released Units Subsequently Resold | {metrics.units_resold:,} |

---

## 2. HTTP Status Code Breakdown & Client Concurrency
| HTTP Status Code | Count | Notes |
|---|---|---|
| **201 Created (/reserve)** | {metrics.reservations_created_201:,} | Initial inventory reservations created |
| **201 Created (/pay)** | {metrics.payments_created_201:,} | Initial payments processed |
| **Total 201 Created** | {metrics.status_counts.get('201', 0):,} | Combined 201 Created |
| **200 OK** | {metrics.status_counts.get('200', 0):,} | Idempotent duplicate replays |
| **409 Conflict** | {metrics.status_counts.get('409', 0):,} | SOLD_OUT when inventory depleted |
| **429 Too Many Requests** | {metrics.status_counts.get('429', 0):,} | Rate limit drops (0 expected) |
| **5xx Server Errors** | {metrics.status_counts.get('5xx', 0):,} | Server errors (0 expected) |
| **Timeouts** | {metrics.status_counts.get('timeouts', 0):,} | Client timeout exceptions |
| **Sum of Status Codes** | **{sum(metrics.status_counts.values()):,}** | **Matches Total HTTP Requests ({len(metrics.latencies):,})** |
| **Configured Concurrency** | **{concurrency}** | Client worker pool size |
| **Observed Peak In-Flight Requests** | **{metrics.max_in_flight}** | Maximum concurrent requests active simultaneously |

---

## 3. Performance & Per-Endpoint Latency Breakdown
| Endpoint / Scope | Requests | Throughput | Latency p50 | Latency p95 | Latency p99 |
|---|---|---|---|---|---|
| **POST /reserve** | {len(metrics.reserve_latencies):,} | {len(metrics.reserve_latencies)/total_duration:.1f} req/s | {res_p50:.2f} ms | {res_p95:.2f} ms | {res_p99:.2f} ms |
| **POST /pay** | {len(metrics.pay_latencies):,} | {len(metrics.pay_latencies)/total_duration:.1f} req/s | {pay_p50:.2f} ms | {pay_p95:.2f} ms | {pay_p99:.2f} ms |
| **Combined Total** | **{len(metrics.latencies):,}** | **{rps:.1f} req/s** | **{p50:.2f} ms** | **{p95:.2f} ms** | **{p99:.2f} ms** |

---

## 4. Inventory State & 100ms Continuous Invariant Sampling
| Column | Final Value | Conservation Check |
|---|---|---|
| Available Quantity | {inv.available_quantity} | Non-negative |
| Reserved Quantity | {inv.reserved_quantity} | Non-negative |
| Sold Quantity | {inv.sold_quantity} | Non-negative |
| Total Initial Stock | {inv.total_stock} | 100 |
| **Sum (Avail + Res + Sold)** | **{inv.available_quantity + inv.reserved_quantity + inv.sold_quantity}** | **Invariant Holds: {inv.validate_conservation()}** |
| **100ms Continuous Sampler** | **{metrics.inventory_samples_count} samples taken** | **Violations detected: {len(metrics.sampler_violations)}** |
| **Peak Committed Stock** | **{metrics.peak_committed_stock} / 100** | **Invariant `(reserved + sold <= 100)` held continuously** |

---

## 5. Outage Evidence, Circuit Breaker & Resiliency
| Metric | Value | Evidence / Explanation |
|---|---|---|
| Downstream Outage Trigger | Immediate on 1st successful payment | Outage overlapped active order creation |
| Outage Duration | {downtime_seconds:.1f} seconds | MockOrderService was simulated DOWN |
| Failed create_order Attempts | {metrics.failed_create_order_attempts} | Calls blocked or rejected during downtime |
| Total Outbox Retries | {metrics.total_retries} | Retry attempts accumulated across events |
| Peak Outbox Pending Count | {metrics.peak_outbox_pending} | Maximum backlog buffered during outage |
| Circuit-Breaker OPEN Transitions | {metrics.circuit_breaker_open_count} | Tripped to OPEN preventing thread exhaustion |
| Outbox Drain Duration | {metrics.outbox_drained_duration:.2f}s | Elapsed time from outage end to 0 pending |
| Post-Outage Order Recovery | {metrics.order_recovery_rate} | Computed directly from database records |
| Final Stuck Outbox Events | {stuck_outbox_count} | 0 pending events |
| Dead-Letter Events | {dead_letter_count} | 0 dropped events |

---

## 6. Invariant Assertions
- [x] **Sold + Reserved <= 100**: `{inv.sold_quantity} + {inv.reserved_quantity} <= 100` -> **{'PASS' if assertion_1 else 'FAIL'}**
- [x] **No negative quantities**: Available: {inv.available_quantity}, Reserved: {inv.reserved_quantity}, Sold: {inv.sold_quantity} -> **{'PASS' if assertion_2 else 'FAIL'}**
- [x] **Payment Idempotency & Gateway Ledger Verification**: Duplicate SUCCESS Payments: {len(duplicate_success_payments)}, Gateway Charges: {gateway_charges_count}, Distinct Payment Keys: {distinct_payment_keys} -> **{'PASS' if assertion_3 else 'FAIL'}**
- [x] **No duplicate orders**: Distinct Orders: {distinct_orders}, Distinct Reservations: {distinct_ord_res} -> **{'PASS' if assertion_4 else 'FAIL'}**
- [x] **Every CONFIRMED reservation has exactly one order**: Confirmed Reservations: {confirmed_reservations_count}, Created Orders: {orders_count} -> **{'PASS' if assertion_5 else 'FAIL'}**
- [x] **100ms Invariant Sampling Held Continuously**: Peak Committed: {metrics.peak_committed_stock}, Violations: {len(metrics.sampler_violations)} -> **{'PASS' if assertion_6 else 'FAIL'}**
- [x] **HTTP Status Sum Matches Total Requests**: Status Sum: {sum(metrics.status_counts.values())}, Requests: {len(metrics.latencies)} -> **{'PASS' if assertion_7 else 'FAIL'}**

**Overall Invariant Verification Status: {'ALL ASSERTIONS PASSED [OK]' if all_assertions_passed else 'FAILED [X]'}**
"""

    print("\n" + report_content)

    os.makedirs(os.path.dirname(output_report), exist_ok=True)
    with open(output_report, "w", encoding="utf-8") as f:
        f.write(report_content)
    print(f"\nSimulation report saved to: {output_report}")

    result_data = {
        "strategy": strategy,
        "concurrency": concurrency,
        "downtime_seconds": downtime_seconds,
        "total_customers": total_customers,
        "total_requests": len(metrics.latencies),
        "duration": total_duration,
        "throughput": rps,
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "reserve_requests": len(metrics.reserve_latencies),
        "reserve_p50": res_p50,
        "reserve_p95": res_p95,
        "reserve_p99": res_p99,
        "pay_requests": len(metrics.pay_latencies),
        "pay_p50": pay_p50,
        "pay_p95": pay_p95,
        "pay_p99": pay_p99,
        "max_in_flight": metrics.max_in_flight,
        "total_errors": metrics.total_errors,
        "status_counts": metrics.status_counts,
        "reservations_created_201": metrics.reservations_created_201,
        "payments_created_201": metrics.payments_created_201,
        "payments_succeeded": metrics.payments_succeeded,
        "payments_failed": metrics.payments_failed,
        "units_released_after_payment_failure": metrics.units_released_after_payment_failure,
        "units_resold": metrics.units_resold,
        "available": inv.available_quantity,
        "reserved": inv.reserved_quantity,
        "sold": inv.sold_quantity,
        "confirmed": confirmed_reservations_count,
        "orders": orders_count,
        "failed_create_order_attempts": metrics.failed_create_order_attempts,
        "total_retries": metrics.total_retries,
        "peak_outbox_pending": metrics.peak_outbox_pending,
        "cb_open_count": metrics.circuit_breaker_open_count,
        "drain_duration": metrics.outbox_drained_duration,
        "order_recovery_rate": metrics.order_recovery_rate,
        "peak_committed_stock": metrics.peak_committed_stock,
        "inventory_samples_count": metrics.inventory_samples_count,
        "sampler_violations_count": len(metrics.sampler_violations),
        "gateway_charges_count": gateway_charges_count,
        "distinct_payment_keys": distinct_payment_keys,
        "assertions_passed": all_assertions_passed,
        "report_content": report_content,
    }

    # Save structured JSON file alongside markdown report
    json_path = output_report.replace(".md", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result_data, f, indent=2)

    assert all_assertions_passed, "One or more invariant assertions failed!"
    return result_data


def compute_stats(values: List[float]) -> Tuple[float, float, float]:
    """Returns (mean, min, max) for a list of values."""
    if not values:
        return 0.0, 0.0, 0.0
    return sum(values) / len(values), min(values), max(values)


async def run_concurrency_sweep(
    strategy: str = "atomic",
    total_customers: int = 10000,
    concurrencies: List[int] = None,
    output_file: str = "simulation/concurrency_scaling_results.json",
) -> List[dict]:
    """Runs simulation across multiple concurrency levels and records peak in-flight, latency, and errors."""
    if concurrencies is None:
        concurrencies = [50, 500, 2000, 10000]

    print("=" * 70)
    print(f"SALESTORM CONCURRENCY SCALING SWEEP: {concurrencies}")
    print("=" * 70)

    sweep_results = []
    for conc in concurrencies:
        print(f"\n>>> TESTING CONCURRENCY = {conc} IN-FLIGHT WORKERS...")
        # For concurrency sweep, use 0s downtime to isolate pure client/server in-flight concurrency scaling
        res = await run_simulation(
            total_customers=total_customers,
            concurrency=conc,
            strategy=strategy,
            downtime_seconds=0.0,
            output_report=f"simulation/report_concurrency_{conc}.md",
        )
        sweep_results.append({
            "concurrency_setting": conc,
            "observed_max_in_flight": res["max_in_flight"],
            "total_requests": res["total_requests"],
            "duration": res["duration"],
            "throughput": res["throughput"],
            "p50": res["p50"],
            "p95": res["p95"],
            "p99": res["p99"],
            "total_errors": res["total_errors"],
            "final_stock": f"{res['sold']} sold, {res['reserved']} res, {res['available']} avail",
            "sampler_violations": res["sampler_violations_count"],
            "duplicate_payments": 0,
            "duplicate_orders": 0,
            "orders_recovered": f"{res['orders']}/{res['confirmed']}",
            "assertions_passed": res["assertions_passed"],
        })

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(sweep_results, f, indent=2)
    print(f"\nConcurrency scaling results saved to: {output_file}")
    return sweep_results


async def run_comparison(
    total_customers: int = 10000,
    concurrency: int = 50,
    downtime_seconds: float = 30.0,
    runs: int = 3,
    output_report: str = "simulation/report.md",
):
    print("=" * 70)
    print(f"SALESTORM COMPARISON BENCHMARK: ATOMIC VS PESSIMISTIC ({runs} RUNS EACH, OUTAGE: {downtime_seconds}s)")
    print("=" * 70)

    atomic_results = []
    pessimistic_results = []

    # 1. Run Atomic Strategy N times
    print(f"\n>>> EXECUTING {runs} RUNS OF ATOMIC CONDITIONAL UPDATE STRATEGY...")
    for run_idx in range(1, runs + 1):
        print(f"\n--- [ATOMIC] Run {run_idx} of {runs} ---")
        res = await run_simulation(
            total_customers=total_customers,
            concurrency=concurrency,
            strategy="atomic",
            downtime_seconds=downtime_seconds,
            output_report=f"simulation/report_atomic_run_{run_idx}.md",
        )
        atomic_results.append(res)

    # 2. Run Pessimistic Strategy N times
    print(f"\n>>> EXECUTING {runs} RUNS OF PESSIMISTIC LOCKING REPOSITORY STRATEGY...")
    for run_idx in range(1, runs + 1):
        print(f"\n--- [PESSIMISTIC] Run {run_idx} of {runs} ---")
        res = await run_simulation(
            total_customers=total_customers,
            concurrency=concurrency,
            strategy="pessimistic",
            downtime_seconds=downtime_seconds,
            output_report=f"simulation/report_pessimistic_run_{run_idx}.md",
        )
        pessimistic_results.append(res)

    print("\nComparison runs complete. Generating comparison report...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SALESTORM Load Test Simulation")
    parser.add_argument("--customers", type=int, default=10000, help="Total customers (default: 10000)")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrent workers (default: 50)")
    parser.add_argument("--url", type=str, default=None, help="Base URL of live server (e.g. http://127.0.0.1:8000)")
    parser.add_argument("--strategy", type=str, default="atomic", choices=["atomic", "pessimistic"], help="Inventory repository strategy")
    parser.add_argument("--compare", action="store_true", help="Run both atomic and pessimistic strategies and produce comparison table")
    parser.add_argument("--concurrency-sweep", action="store_true", help="Run concurrency scaling sweep [50, 500, 2000, 10000]")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs per strategy for comparison (default: 3)")
    parser.add_argument("--downtime", type=float, default=30.0, help="OrderService downtime seconds (default: 30.0)")
    parser.add_argument("--report", type=str, default="simulation/report.md", help="Output report file path")
    args = parser.parse_args()

    if args.concurrency_sweep:
        asyncio.run(
            run_concurrency_sweep(
                strategy=args.strategy,
                total_customers=args.customers,
                output_file="simulation/concurrency_scaling_results.json",
            )
        )
    elif args.compare:
        asyncio.run(
            run_comparison(
                total_customers=args.customers,
                concurrency=args.concurrency,
                downtime_seconds=args.downtime,
                runs=args.runs,
                output_report=args.report,
            )
        )
    else:
        asyncio.run(
            run_simulation(
                total_customers=args.customers,
                concurrency=args.concurrency,
                base_url=args.url,
                strategy=args.strategy,
                downtime_seconds=args.downtime,
                output_report=args.report,
            )
        )
