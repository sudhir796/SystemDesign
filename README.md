# SALESTORM: High-Concurrency Flash Sale Prototype

SALESTORM is an enterprise-grade prototype designed for a high-concurrency flash sale system. It models extreme traffic spikes (10,000+ concurrent requests for limited inventory) while demonstrating distributed systems patterns: atomic stock reservation, strict idempotency, transactional outbox messaging with circuit breaker resilience, and automated stock reclamation.

---

## 1. Tech Stack & Environment

- **Language / Runtime**: Python 3.10+ / 3.12 within isolated virtual environment (`.venv`)
- **API Framework**: FastAPI, Uvicorn (ASGI)
- **Database Engine**: SQLite 3 configured with Write-Ahead Logging (`WAL` mode)
  - Pragmas: `journal_mode = WAL`, `synchronous = NORMAL`, `busy_timeout = 30000`, `foreign_keys = ON`
  - Engine `CHECK` constraints ensuring conservation of stock: `available_quantity >= 0`, `reserved_quantity >= 0`, `sold_quantity >= 0`, and `available + reserved + sold = total_stock`
- **Asynchronous HTTP & Load Testing**: Asyncio, HTTPX
- **Automated Testing**: Pytest, Pytest-Asyncio
- **Data Validation & State Machine**: Pydantic v2, Python Dataclasses, State Design Pattern

---

## 2. Project Architecture

```text
d:/System_Design/
├── app/
│   ├── api/
│   │   ├── reservation_router.py   # POST /reserve with mandatory Idempotency-Key
│   │   ├── payment_router.py       # POST /pay with status reconciliation
│   │   └── schemas.py              # Pydantic request/response schemas
│   ├── models/
│   │   ├── inventory.py            # Inventory entity with balance invariants
│   │   ├── reservation.py          # InventoryReservation entity & states
│   │   ├── payment.py              # Payment entity & transaction statuses
│   │   ├── order.py                # Order entity with unique reservation linkage
│   │   ├── outbox.py               # OutboxEvent entity (Transactional Outbox Pattern)
│   │   └── order_state.py          # State pattern enforcing valid order lifecycle
│   ├── repositories/
│   │   ├── inventory_repo.py       # SqliteInventoryRepository (single atomic UPDATE)
│   │   ├── pessimistic_repo.py     # PessimisticInventoryRepository (mutex lock simulation)
│   │   ├── reservation_repo.py     # Reservation persistence & idempotency lookups
│   │   ├── payment_repo.py         # Payment persistence
│   │   ├── order_repo.py           # Order persistence & unique constraint enforcement
│   │   └── outbox_repo.py          # Outbox polling, event locking & status updates
│   ├── services/
│   │   ├── reservation_service.py  # Stock reservation orchestrator & fast-path check
│   │   ├── payment_gateway.py      # MockPaymentGateway (95% success, 5% failure, timeouts)
│   │   ├── payment_service.py      # Atomic payment settlement & outbox emission
│   │   ├── payment_reconciliation.py # Resolves UNKNOWN timeout payments
│   │   ├── circuit_breaker.py      # 3-state Circuit Breaker (CLOSED, OPEN, HALF_OPEN)
│   │   └── order_service.py        # OrderService with 30s simulated outage toggle
│   ├── workers/
│   │   ├── outbox_worker.py        # Background outbox worker with retries & dead-letter queue
│   │   └── expiry_worker.py        # High-concurrency TTL reaper with multi-worker safe updates
│   ├── config.py                   # Centralized configuration & repository strategy selection
│   ├── db.py                       # Connection manager, WAL pragmas, schema migration
│   └── main.py                     # FastAPI app with health checks & simulation admin routes
├── scripts/
│   └── seed.py                     # Initial database seeder (Product X = 100 units)
├── simulation/
│   ├── load_test.py                # 10,000-customer simulation harness & comparative benchmark
│   └── report.md                   # Generated performance and correctness report
├── tests/
│   ├── test_hackathon_scenarios.py # 6 mandatory hackathon distributed system scenario tests
│   ├── test_inventory.py           # Atomic vs pessimistic repository unit tests
│   ├── test_reservation.py         # Idempotency and TTL reservation tests
│   ├── test_payment.py             # Payment gateway and stock release tests
│   ├── test_order_outbox.py        # Outbox worker and circuit breaker tests
│   └── test_state_machine.py       # Order state machine validation tests
├── TEAM 4 - System Design.pdf  # Comprehensive architecture & system design document (11 visual diagrams)
├── Requirements.pdf            # Hackathon problem statement and technical requirements
├── SALESTORM - ER Diagram (Reference Layout).pdf # Reference ER schema layout
├── AI_USAGE_NOTE.md            # AI transparency, generated code, and human review notes
└── README.md
```

---

## 2.1 System Design & Architecture Documents

The repository includes the complete system design package and specifications:
- [TEAM 4 - System Design.pdf](file:///d:/System_Design/TEAM%204%20-%20System%20Design.pdf): Complete architecture package containing 11 formal architectural diagrams:
- [Requirements.pdf](file:///d:/System_Design/Requirements.pdf): Hackathon problem statement, constraints, and engineering requirements.
- [SALESTORM - ER Diagram (Reference Layout).pdf](file:///d:/System_Design/SALESTORM%20-%20ER%20Diagram%20(Reference%20Layout).pdf): Detailed reference entity-relationship layout for inventory, order, and payment models.

### Architectural Diagrams Overview:
1. **System Context Diagram** (Context boundary: Customer, SaleStorm Platform, External Gateways, Shipping & Notifications)
2. **High-Level Design (HLD)** (Edge, API Gateway, Microservices, Shared Infrastructure, Observability)
3. **Container Diagram (C4 Level 2)** (CDN/WAF, Admission/Waiting Room, Redis Cluster, PostgreSQL, Kafka Message Broker, Order DB)
4. **Component Diagram (C4 Level 3)** (ReservationController, ReservationService, StockGate interface, RedisStockGate Lua atomic DECR, ExpiryWorker, Outbox EventPublisher)
5. **Deployment Diagram** (Kubernetes Pods across Multi-AZ, Primary/Standby Postgres with Sync Replication, Kafka RF=3)
6. **Entity Relationship (ER) Diagram** (Customer, Order, OrderItem, Reservation, Inventory, Payment, Outbox schema relationships)
7. **Class Diagram** (CheckoutFacade, ReservationService, PaymentService, OrderService, Strategy interfaces)
8. **Purchase / Inventory Reservation Sequence Diagram** (Admission queue, Redis Lua atomic decrement, conditional DB UPDATE fallback, idempotency handling)
9. **Payment Sequence Diagram** (Idempotency check, external gateway call, outbox event emission, reconciliation worker)
10. **Order Sequence and Recovery Diagram** (Kafka consumer, Order DB inbox deduplication, dead-letter queue, automated compensation/refunds)
11. **Reservation State Diagram** (AVAILABLE -> RESERVED -> PAYMENT_PENDING -> CONFIRMED -> SOLD / EXPIRED / RELEASED)

---

## 3. Core Architectural Mechanisms

### A. Atomic Conditional Update vs Pessimistic Locking
- **Atomic-Update (`SqliteInventoryRepository`)**:
  Executes a single conditional atomic statement:
  ```sql
  UPDATE inventory
  SET available_quantity = available_quantity - 1,
      reserved_quantity = reserved_quantity + 1,
      version = version + 1,
      updated_at = CURRENT_TIMESTAMP
  WHERE product_id = ? AND available_quantity > 0;
  ```
  - `rowcount == 1`: Stock reserved successfully.
  - `rowcount == 0`: Stock depleted (`409 SOLD_OUT`).
  - Pushes concurrency checks directly down to SQLite engine. No application-level read-then-write locks required. Readers never block writers under WAL mode.
- **Pessimistic Locking (`PessimisticInventoryRepository`)**:
  Simulates `SELECT ... FOR UPDATE` row locking using a mutual exclusion lock (`threading.Lock`) guarding read-then-write logic. Selectable via `INVENTORY_STRATEGY=pessimistic`.

### B. Two-Layer Idempotency Guarantees
- **Mandatory Header**: Every `/reserve` and `/pay` request requires an `Idempotency-Key` HTTP header. Missing headers are rejected with `400 Bad Request`.
- **Fast-Path Memory/DB Check**: Existing keys immediately return the original record with `200 OK` without touching inventory balances.
- **Concurrent Race Protection**: Concurrent identical requests race to commit. The loser trips the database `UNIQUE(idempotency_key)` constraint, rolls back, and returns the winner's reservation.

### C. Transactional Outbox Pattern & Circuit Breaker
- Direct distributed synchronous calls between Payment and Order services risk distributed state inconsistency if network drops occur.
- When payment succeeds, payment state, inventory update, and an `outbox_event` (`ORDER_REQUESTED`) are committed in the **same local transaction**.
- An asynchronous `OutboxWorker` drains pending events and forwards them to `OrderService.create_order`.
- Calls are wrapped by a 3-state **Circuit Breaker** (`CLOSED` -> `OPEN` -> `HALF_OPEN`). When downstream services fail or experience simulated 30-second downtime, the circuit trips `OPEN`, preventing thread pool exhaustion and cascading failure. Once restored, the circuit resets and pending events are drained.

### D. Multi-Worker Safe Stock Expiry
- Stale reservations past TTL (default: 10 minutes; tests: 5 seconds) are harvested by `ExpiryWorker`.
- Employs conditional update `WHERE reservation_id = ? AND status IN ('RESERVED', 'PAYMENT_PENDING')`.
- If multiple worker nodes run simultaneously, only one instance gets `rowcount == 1` and restores stock (`reserved - 1`, `available + 1`), preventing double-reclaim race conditions.

---

## 4. How to Run

### Step 1: Virtual Environment Setup
Ensure all commands are executed within the virtual environment:
```powershell
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies (if not already installed)
pip install -r requirements.txt
```

### Step 2: Run the Pytest Test Suite
Execute the entire test suite, including the 6 hackathon scenario tests:
```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

### Step 3: Run the Flash Sale Simulation
Run the 10,000-customer simulation with simulated 30-second OrderService outage:
```powershell
# Run with Atomic-Update strategy (default)
.\.venv\Scripts\python.exe simulation/load_test.py --customers 10000 --downtime 30 --concurrency 50

# Run comparative benchmark (Atomic-Update vs Pessimistic Locking)
.\.venv\Scripts\python.exe simulation/load_test.py --compare --customers 10000 --downtime 30 --concurrency 50
```
Simulation outputs performance metrics and invariant verification directly to `simulation/report.md`.

### Step 4: Run the Live FastAPI Server
```powershell
# Start server in Atomic mode
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload

# Or start server in Pessimistic mode
$env:INVENTORY_STRATEGY="pessimistic"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
```

### Step 5: Execute API Requests via PowerShell
**Create a Reservation:**
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/reserve" `
  -Method POST `
  -Headers @{ "Idempotency-Key" = "client-tx-001" } `
  -ContentType "application/json" `
  -Body '{"product_id": "PRODUCT_X", "customer_id": "cust_101"}'
```

**Pay for Reservation:**
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/pay" `
  -Method POST `
  -Headers @{ "Idempotency-Key" = "pay-tx-001" } `
  -ContentType "application/json" `
  -Body '{"reservation_id": "<RESERVATION_ID>"}'
```

---

## 5. Design Assumptions & Validation Matrix

Each test and simulation scenario explicitly validates a fundamental assumption of distributed system design:

| Scenario / Test | File & Function | Design Assumption Validated | Verification Mechanism |
|---|---|---|---|
| **1. Last-Unit Race** | `tests/test_hackathon_scenarios.py::<br>test_last_unit_race_two_concurrent_requests` | **Zero-Oversell Under Maximum Contention**: When inventory is down to 1 unit, two concurrent requests at the exact same instant must result in exactly 1 winner (`201 Created`) and 1 loser (`409 SOLD_OUT`). | Executes 2 parallel threads against 1 unit of stock; verifies database invariants: `available=0`, `reserved=1`, `sold=0`, total reservations = 1. |
| **2. Idempotent Duplicate** | `tests/test_hackathon_scenarios.py::<br>test_idempotent_duplicate_request` | **At-Least-Once Network Delivery Safety**: Client network retries sending duplicate `Idempotency-Key` headers must never double-decrement stock or create duplicate reservations. | Sends identical request sequentially and concurrently; asserts original `reservation_id` is returned with `200 OK` and stock is decremented exactly once. |
| **3. Payment Failure Releases Stock** | `tests/test_hackathon_scenarios.py::<br>test_payment_failure_releases_stock` | **Transactional Compensating Actions**: If an external payment gateway rejects payment (5% failure rate), reserved stock must be reliably restored to `available_quantity`. | Forces `MockPaymentGateway` to fail; asserts reservation moves to `RELEASED`, payment records `FAILED`, and stock shifts from `reserved` back to `available`. |
| **4. Expiry Worker Releases Stock** | `tests/test_hackathon_scenarios.py::<br>test_expiry_releases_stock` | **Time-To-Live (TTL) Self-Healing**: Abandoned carts must expire after TTL and release stock back to the public pool; multiple simultaneous expiry workers must not double-release. | Seeds expired reservation (`expires_at < now`); executes two parallel `ExpiryWorker.process_expired_batch()` sweeps; asserts single transition to `EXPIRED` and exactly 1 stock unit restored. |
| **5. Order Created After Outage** | `tests/test_hackathon_scenarios.py::<br>test_order_created_after_outage` | **Eventual Consistency & Fault Tolerance**: Downstream service crashes (30s outage) must not cause data loss. Outbox events must buffer durably and resume automatically upon service recovery. | Takes `MockOrderService` down; outbox worker backs off and circuit breaker trips `OPEN`; restores service and calls `retry_dead_letter()`; verifies order is successfully created with `UNIQUE(reservation_id)`. |
| **6. Invalid State Transition Rejected** | `tests/test_hackathon_scenarios.py::<br>test_invalid_state_transition_rejected` | **State Machine Integrity**: Domain orders must follow a strict acyclic lifecycle (`CREATED` -> `PAYMENT_PENDING` -> `CONFIRMED` -> `PROCESSING` -> `SHIPPED` -> `OUT_FOR_DELIVERY` -> `DELIVERED`). | Attempts illegal transitions (e.g. `CREATED` -> `DELIVERED` or transitioning out of terminal `DELIVERED`); asserts `InvalidStateTransitionError` is thrown. |
| **7. 200 Concurrent Requests (Same Key)** | `tests/test_hackathon_scenarios.py::<br>test_200_concurrent_reserve_calls_same_idempotency_key` | **Extreme Idempotent Contention**: 200 concurrent threads hitting `/reserve` simultaneously with the identical `Idempotency-Key` while stock is available. | Verifies: exactly 1 reservation created in DB, stock decremented by exactly 1 unit (`available = 99`, `reserved = 1`), and all 200 responses return the exact same `reservation_id`. |
| **8. 50 Concurrent Payments (Same Key)** | `tests/test_hackathon_scenarios.py::<br>test_50_concurrent_pay_calls_same_idempotency_key` | **Payment Ledger & Idempotency Safety**: 50 concurrent threads hitting `/pay` simultaneously with the same `Idempotency-Key` for an active reservation. | Verifies: exactly 1 payment record created in DB, exactly 1 charge executed in payment gateway ledger, and stock moves atomically from `reserved` to `sold`. |
| **9. 10,000-Customer Flash Sale & Sampler** | `simulation/load_test.py::<br>run_simulation()` | **End-to-End System Invariant Stability & Continuous Conservation**: System sustains 10k requests, 2% duplicates, and downstream outage. A background sampler checks inventory every 100ms. | Verifies: continuous 100ms sampling confirms `reserved + sold <= 100` at every single sample, no negative balances, zero duplicate payments/orders, full HTTP status breakdown summing to total requests, and all confirmed reservations yield orders. |

---

## 6. Known Limitations & Production Evolution

While SALESTORM proves the validity of the core algorithms and concurrency contracts, real-world hyperscale deployments introduce physical infrastructure constraints:

### 1. Single-Node SQLite vs Sharded Relational Database
- **Current Limitation**: SQLite in WAL mode permits concurrent readers, but all write transactions acquire an exclusive file lock (`PAGER_LOCK`). Under tens of thousands of write requests per second, this creates lock contention and limits throughput.
- **Production Architecture**:
  - Replace SQLite with an enterprise relational database (e.g., PostgreSQL or CockroachDB).
  - Horizontally partition inventory by `hash(product_id)` across distinct database shards. Each shard processes transactions independently without cross-shard lock contention.

### 2. Database Inventory Check vs Distributed In-Memory Cache (Redis)
- **Current Limitation**: Every reservation request queries and updates the database directly.
- **Production Architecture**:
  - Offload initial inventory decrements to an in-memory Redis cluster.
  - Execute an atomic Lua script on Redis:
    ```lua
    local stock = redis.call('get', KEYS[1])
    if tonumber(stock) > 0 then
      redis.call('decr', KEYS[1])
      return 1
    else
      return 0
    end
    ```
  - This absorbs 500,000+ RPS at sub-millisecond latencies, asynchronous writes are buffered, and only winners reach the database to create reservations.

### 3. In-Process Outbox Polling vs Change Data Capture (CDC) & Kafka
- **Current Limitation**: `OutboxWorker` periodically polls the `outbox_event` SQLite table (`SELECT ... WHERE status = 'PENDING' LIMIT 50`), introducing polling overhead and database read traffic.
- **Production Architecture**:
  - Utilize Debezium CDC to tail the database transaction write-ahead log (PostgreSQL WAL) directly.
  - Stream events with zero polling overhead into an Apache Kafka topic partitioned by `customer_id` or `order_id`.
  - Worker microservices consume Kafka partitions with guaranteed at-least-once delivery and horizontal auto-scaling.

### 4. Distributed Consensus & Multi-Region Active-Active
- **Current Limitation**: Runs in a single node / process space.
- **Production Architecture**:
  - Multi-region flash sales require distributed consensus (Raft or Spanner TrueTime) to prevent split-brain inventory allocation across geographic zones.
  - Inventory is pre-allocated regionally (e.g., 40 units US-East, 30 units EU-West, 30 units AP-South) with localized reservation pools.
