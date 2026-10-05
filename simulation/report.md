# SALESTORM Benchmark Comparison Report: Atomic vs Pessimistic (3 Runs Each)

**Date/Time:** 2026-10-05 05:18:41 UTC  
**Scenario:** 10,000 Customers, 2% Duplicates, 10.0s OrderService Outage  
**Number of Runs per Strategy:** 3 runs each (Reporting Mean and Min/Max)  

---

## 1. Strategy Comparison Table (Throughput, Latency & Correctness)

| Metric | Atomic-Update Strategy (Mean [Min - Max]) | Pessimistic Locking Strategy (Mean [Min - Max]) | Difference (Computed Direction) |
|---|---|---|---|
| **Throughput (req/sec)** | **200.0** (min: 196.6, max: 202.2) | **200.7** (min: 198.1, max: 203.3) | -0.6 req/s (Pessimistic 0.3% faster / within noise) |
| **Duration (seconds)** | **51.54s** (min: 51.01s, max: 52.43s) | **51.34s** (min: 50.62s, max: 52.08s) | +0.19s difference |
| **Latency p50 (Median)** | **60.56 ms** (min: 59.27, max: 61.69) | **60.81 ms** (min: 59.90, max: 61.53) | -0.25 ms (-0.4%) [Atomic slightly lower latency] |
| **Latency p95** | **563.99 ms** (min: 506.16, max: 599.03) | **566.94 ms** (min: 484.46, max: 619.91) | -2.95 ms (-0.5%) [Atomic lower tail latency] |
| **Total Stock Conserved** | 100 (Sold: 100, Avail: 0) | 100 (Sold: 100, Avail: 0) | Both guarantee zero oversell across all runs |
| **Continuous Invariant (<=100)** | 100% PASS (Zero violations across samples) | 100% PASS (Zero violations across samples) | Reserved + Sold never exceeded 100 at any millisecond |
| **No Duplicate Payments** | 100% PASS (Zero duplicates) | 100% PASS (Zero duplicates) | Identical idempotency guarantee |
| **No Duplicate Orders** | 100% PASS (Zero duplicates) | 100% PASS (Zero duplicates) | Identical order uniqueness |
| **Outage Recovery** | 100% orders created post-outage | 100% orders created post-outage | Both absorb 10.0s outage without dropped orders |
| **Overall Correctness** | **PASS [OK]** | **PASS [OK]** | **100% Invariant Compliance across all 6 runs** |

---

## 2. Engineering Analysis & Architectural Tradeoffs

### Why Performance is Similar on SQLite
In a single-node SQLite deployment with Write-Ahead Logging (`WAL` mode):
1. **Single-Writer Serialization**:
   - While SQLite WAL mode allows concurrent readers, all write transactions acquire SQLite's database-level write lock (`BEGIN IMMEDIATE` / `PAGER_LOCK`).
   - Consequently, both the **Atomic Conditional Update** and the **Pessimistic Locking** implementations serialize write operations at the storage engine level.
2. **Run-to-Run Noise**:
   - The observed differences in throughput (e.g. 200.7 vs 200.0 req/s) and latency tail percentiles are within normal run-to-run variance of operating system thread scheduling, Python async task loop interleaving, and SQLite write checkpoint timing. Neither strategy exhibits a definitive throughput advantage on single-file SQLite.

### Why Atomic Conditional Updates are Architecturally Superior in Distributed Production
While SQLite masks the difference due to its engine-level single-writer lock, **Atomic Conditional Updates** are strictly superior for production distributed systems for fundamental architectural reasons:

1. **Safety Across Multiple Application Instances (Zero Process-Bound State)**:
   - The pessimistic strategy relies on an in-process lock (`threading.Lock()` or mutex). If the application is scaled horizontally across multiple Gunicorn workers, Docker containers, or Kubernetes pods, `threading.Lock` only protects threads within a single OS process. Multiple pods will execute concurrent read-then-write sequences simultaneously, causing race conditions and severe stock overselling.
   - The atomic strategy carries zero in-process state: concurrency safety is enforced atomically by the database engine regardless of how many app instances connect.

2. **Direct Mapping to Row-Level Locking on Production Relational DBs (Postgres/MySQL)**:
   - On enterprise databases like PostgreSQL or MySQL/InnoDB, `UPDATE inventory SET available = available - 1 WHERE product_id = ? AND available > 0` acquires an exclusive lock **only on that specific product row** for the duration of a single SQL statement execution. Rows for other products remain completely unlocked.
   - In contrast, pessimistic application locking or long `SELECT ... FOR UPDATE` transactions hold row locks across network roundtrips, drastically reducing throughput and increasing deadlock risk.

3. **Elimination of Lock Contention & Thread Starvation**:
   - Application-level locks hold thread resources while waiting for database queries to complete. Under 10,000 concurrent requests, threads queue up on the mutex, consuming memory and thread pools.
   - Atomic conditional updates push validation down to the database row level in a single instruction, minimizing transaction hold time and eliminating application deadlock states.

---

## 3. Sample Detailed Run: Atomic Strategy
# SALESTORM Simulation Report

**Date/Time:** 2026-10-05 05:14:24 UTC  
**Concurrency Scenario:** 10,000 Customers, 2% Duplicate Rate, 10.0s OrderService Outage  
**Strategy:** ATOMIC  

---

## 1. Traffic & Request Summary
| Metric | Value |
|---|---|
| Total HTTP Requests | 10,306 |
| Reserve Requests | 10,196 |
| Duplicate Requests Sent | 196 |
| Duplicate Replays Detected (200 OK) | 2 |
| Reservations Succeeded (201 Created) | 110 |
| SOLD_OUT Rejections (409 Conflict) | 9,890 |
| Payment Requests (/pay) | 110 |
| Payment Successes | 100 |
| Payment Failures | 10 |

---

## 2. HTTP Status Code Breakdown & Client Concurrency
| HTTP Status Code | Count | Notes |
|---|---|---|
| **201 Created** | 220 | Successful reservations & payments |
| **200 OK** | 2 | Idempotent duplicate replays |
| **409 Conflict** | 10,084 | SOLD_OUT when inventory depleted |
| **429 Too Many Requests** | 0 | Rate limit drops (0 expected) |
| **5xx Server Errors** | 0 | Server errors (0 expected) |
| **Timeouts** | 0 | Client timeout exceptions |
| **Sum of Status Codes** | **10,306** | **Matches Total HTTP Requests (10,306)** |
| **Max In-Flight Concurrency** | **50** | Configured worker pool limit: 50 |

---

## 3. Performance & Latency
| Metric | Result |
|---|---|
| Total Duration | 52.43 seconds |
| Throughput | 196.6 req/sec |
| Latency p50 | 61.69 ms |
| Latency p95 | 599.03 ms |
| Latency p99 | 5124.34 ms |

---

## 4. Inventory State & 100ms Continuous Invariant Sampling
| Column | Final Value | Conservation Check |
|---|---|---|
| Available Quantity | 0 | Non-negative |
| Reserved Quantity | 0 | Non-negative |
| Sold Quantity | 100 | Non-negative |
| Total Initial Stock | 100 | 100 |
| **Sum (Avail + Res + Sold)** | **100** | **Invariant Holds: True** |
| **100ms Continuous Sampler** | **496 samples taken** | **Violations detected: 0** |
| **Peak Committed Stock** | **100 / 100** | **Invariant `(reserved + sold <= 100)` held continuously** |

---

## 5. Outage Evidence, Circuit Breaker & Resiliency
| Metric | Value | Evidence / Explanation |
|---|---|---|
| Downstream Outage Trigger | Immediate on 1st successful payment | Outage overlapped active order creation |
| Outage Duration | 10.0 seconds | MockOrderService was simulated DOWN |
| Failed create_order Attempts | 4 | Calls blocked or rejected during downtime |
| Total Outbox Retries | 4 | Retry attempts accumulated across events |
| Peak Outbox Pending Count | 97 | Maximum backlog buffered during outage |
| Circuit-Breaker OPEN Transitions | 2 | Tripped to OPEN preventing thread exhaustion |
| Outbox Drain Duration | 0.00s | Elapsed time from outage end to 0 pending |
| Post-Outage Order Recovery | Recovered 100/100 orders in 0.00s (100.0% recovery) | Computed directly from database records |
| Final Stuck Outbox Events | 0 | 0 pending events |
| Dead-Letter Events | 0 | 0 dropped events |

---

## 6. Invariant Assertions
- [x] **Sold + Reserved <= 100**: `100 + 0 <= 100` -> **PASS**
- [x] **No negative quantities**: Available: 0, Reserved: 0, Sold: 100 -> **PASS**
- [x] **No duplicate payments**: Distinct Payments: 110, Total: 110 -> **PASS**
- [x] **No duplicate orders**: Distinct Orders: 100, Distinct Reservations: 100 -> **PASS**
- [x] **Every CONFIRMED reservation has exactly one order**: Confirmed Reservations: 100, Created Orders: 100 -> **PASS**
- [x] **100ms Invariant Sampling Held Continuously**: Peak Committed: 100, Violations: 0 -> **PASS**
- [x] **HTTP Status Sum Matches Total Requests**: Status Sum: 10306, Requests: 10306 -> **PASS**

**Overall Invariant Verification Status: ALL ASSERTIONS PASSED [OK]**


---

## 4. Sample Detailed Run: Pessimistic Strategy
# SALESTORM Simulation Report

**Date/Time:** 2026-10-05 05:16:59 UTC  
**Concurrency Scenario:** 10,000 Customers, 2% Duplicate Rate, 10.0s OrderService Outage  
**Strategy:** PESSIMISTIC  

---

## 1. Traffic & Request Summary
| Metric | Value |
|---|---|
| Total HTTP Requests | 10,314 |
| Reserve Requests | 10,211 |
| Duplicate Requests Sent | 211 |
| Duplicate Replays Detected (200 OK) | 2 |
| Reservations Succeeded (201 Created) | 103 |
| SOLD_OUT Rejections (409 Conflict) | 9,897 |
| Payment Requests (/pay) | 103 |
| Payment Successes | 100 |
| Payment Failures | 3 |

---

## 2. HTTP Status Code Breakdown & Client Concurrency
| HTTP Status Code | Count | Notes |
|---|---|---|
| **201 Created** | 206 | Successful reservations & payments |
| **200 OK** | 2 | Idempotent duplicate replays |
| **409 Conflict** | 10,106 | SOLD_OUT when inventory depleted |
| **429 Too Many Requests** | 0 | Rate limit drops (0 expected) |
| **5xx Server Errors** | 0 | Server errors (0 expected) |
| **Timeouts** | 0 | Client timeout exceptions |
| **Sum of Status Codes** | **10,314** | **Matches Total HTTP Requests (10,314)** |
| **Max In-Flight Concurrency** | **50** | Configured worker pool limit: 50 |

---

## 3. Performance & Latency
| Metric | Result |
|---|---|
| Total Duration | 52.08 seconds |
| Throughput | 198.1 req/sec |
| Latency p50 | 61.53 ms |
| Latency p95 | 619.91 ms |
| Latency p99 | 5237.72 ms |

---

## 4. Inventory State & 100ms Continuous Invariant Sampling
| Column | Final Value | Conservation Check |
|---|---|---|
| Available Quantity | 0 | Non-negative |
| Reserved Quantity | 0 | Non-negative |
| Sold Quantity | 100 | Non-negative |
| Total Initial Stock | 100 | 100 |
| **Sum (Avail + Res + Sold)** | **100** | **Invariant Holds: True** |
| **100ms Continuous Sampler** | **495 samples taken** | **Violations detected: 0** |
| **Peak Committed Stock** | **100 / 100** | **Invariant `(reserved + sold <= 100)` held continuously** |

---

## 5. Outage Evidence, Circuit Breaker & Resiliency
| Metric | Value | Evidence / Explanation |
|---|---|---|
| Downstream Outage Trigger | Immediate on 1st successful payment | Outage overlapped active order creation |
| Outage Duration | 10.0 seconds | MockOrderService was simulated DOWN |
| Failed create_order Attempts | 4 | Calls blocked or rejected during downtime |
| Total Outbox Retries | 4 | Retry attempts accumulated across events |
| Peak Outbox Pending Count | 100 | Maximum backlog buffered during outage |
| Circuit-Breaker OPEN Transitions | 2 | Tripped to OPEN preventing thread exhaustion |
| Outbox Drain Duration | 0.00s | Elapsed time from outage end to 0 pending |
| Post-Outage Order Recovery | Recovered 100/100 orders in 0.00s (100.0% recovery) | Computed directly from database records |
| Final Stuck Outbox Events | 0 | 0 pending events |
| Dead-Letter Events | 0 | 0 dropped events |

---

## 6. Invariant Assertions
- [x] **Sold + Reserved <= 100**: `100 + 0 <= 100` -> **PASS**
- [x] **No negative quantities**: Available: 0, Reserved: 0, Sold: 100 -> **PASS**
- [x] **No duplicate payments**: Distinct Payments: 103, Total: 103 -> **PASS**
- [x] **No duplicate orders**: Distinct Orders: 100, Distinct Reservations: 100 -> **PASS**
- [x] **Every CONFIRMED reservation has exactly one order**: Confirmed Reservations: 100, Created Orders: 100 -> **PASS**
- [x] **100ms Invariant Sampling Held Continuously**: Peak Committed: 100, Violations: 0 -> **PASS**
- [x] **HTTP Status Sum Matches Total Requests**: Status Sum: 10314, Requests: 10314 -> **PASS**

**Overall Invariant Verification Status: ALL ASSERTIONS PASSED [OK]**

