# SALESTORM Simulation Report

**Date/Time:** 2026-10-05 06:28:40 UTC  
**Concurrency Scenario:** 10,000 Customers, 2% Duplicate Rate, 0.0s OrderService Outage  
**Strategy:** ATOMIC | **Configured Concurrency:** 500 (Observed Peak In-Flight: 500)  

---

## 1. Traffic & Request Summary
| Metric | Value |
|---|---|
| Total HTTP Requests | 10,325 |
| Reserve Requests | 10,223 |
| Duplicate Requests Sent | 223 |
| Duplicate Replays Detected (200 OK) | 1 |
| Reservations Succeeded (201 Created) | 102 |
| SOLD_OUT Rejections (409 Conflict) | 9,898 |
| Payment Requests (/pay) | 102 |
| Payment Successes | 100 |
| Payment Failures | 2 |
| Units Released after Payment Failure | 2 |
| Released Units Subsequently Resold | 2 |

---

## 2. HTTP Status Code Breakdown & Client Concurrency
| HTTP Status Code | Count | Notes |
|---|---|---|
| **201 Created (/reserve)** | 102 | Initial inventory reservations created |
| **201 Created (/pay)** | 102 | Initial payments processed |
| **Total 201 Created** | 204 | Combined 201 Created |
| **200 OK** | 1 | Idempotent duplicate replays |
| **409 Conflict** | 10,120 | SOLD_OUT when inventory depleted |
| **429 Too Many Requests** | 0 | Rate limit drops (0 expected) |
| **5xx Server Errors** | 0 | Server errors (0 expected) |
| **Timeouts** | 0 | Client timeout exceptions |
| **Sum of Status Codes** | **10,325** | **Matches Total HTTP Requests (10,325)** |
| **Configured Concurrency** | **500** | Client worker pool size |
| **Observed Peak In-Flight Requests** | **500** | Maximum concurrent requests active simultaneously |

---

## 3. Performance & Per-Endpoint Latency Breakdown
| Endpoint / Scope | Requests | Throughput | Latency p50 | Latency p95 | Latency p99 |
|---|---|---|---|---|---|
| **POST /reserve** | 10,223 | 330.0 req/s | 1237.15 ms | 2587.92 ms | 4330.09 ms |
| **POST /pay** | 102 | 3.3 req/s | 4071.22 ms | 5594.71 ms | 11613.33 ms |
| **Combined Total** | **10,325** | **333.3 req/s** | **1238.10 ms** | **2681.96 ms** | **4521.85 ms** |

---

## 4. Inventory State & 100ms Continuous Invariant Sampling
| Column | Final Value | Conservation Check |
|---|---|---|
| Available Quantity | 0 | Non-negative |
| Reserved Quantity | 0 | Non-negative |
| Sold Quantity | 100 | Non-negative |
| Total Initial Stock | 100 | 100 |
| **Sum (Avail + Res + Sold)** | **100** | **Invariant Holds: True** |
| **100ms Continuous Sampler** | **289 samples taken** | **Violations detected: 0** |
| **Peak Committed Stock** | **100 / 100** | **Invariant `(reserved + sold <= 100)` held continuously** |

---

## 5. Outage Evidence, Circuit Breaker & Resiliency
| Metric | Value | Evidence / Explanation |
|---|---|---|
| Downstream Outage Trigger | Immediate on 1st successful payment | Outage overlapped active order creation |
| Outage Duration | 0.0 seconds | MockOrderService was simulated DOWN |
| Failed create_order Attempts | 0 | Calls blocked or rejected during downtime |
| Total Outbox Retries | 0 | Retry attempts accumulated across events |
| Peak Outbox Pending Count | 27 | Maximum backlog buffered during outage |
| Circuit-Breaker OPEN Transitions | 0 | Tripped to OPEN preventing thread exhaustion |
| Outbox Drain Duration | 0.00s | Elapsed time from outage end to 0 pending |
| Post-Outage Order Recovery | Recovered 100/100 orders in 0.00s (100.0% recovery) | Computed directly from database records |
| Final Stuck Outbox Events | 0 | 0 pending events |
| Dead-Letter Events | 0 | 0 dropped events |

---

## 6. Invariant Assertions
- [x] **Sold + Reserved <= 100**: `100 + 0 <= 100` -> **PASS**
- [x] **No negative quantities**: Available: 0, Reserved: 0, Sold: 100 -> **PASS**
- [x] **Payment Idempotency & Gateway Ledger Verification**: Duplicate SUCCESS Payments: 0, Gateway Charges: 102, Distinct Payment Keys: 102 -> **PASS**
- [x] **No duplicate orders**: Distinct Orders: 100, Distinct Reservations: 100 -> **PASS**
- [x] **Every CONFIRMED reservation has exactly one order**: Confirmed Reservations: 100, Created Orders: 100 -> **PASS**
- [x] **100ms Invariant Sampling Held Continuously**: Peak Committed: 100, Violations: 0 -> **PASS**
- [x] **HTTP Status Sum Matches Total Requests**: Status Sum: 10325, Requests: 10325 -> **PASS**

**Overall Invariant Verification Status: ALL ASSERTIONS PASSED [OK]**
