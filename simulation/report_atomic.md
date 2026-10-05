# SALESTORM Simulation Report

**Date/Time:** 2026-10-05 04:57:50 UTC  
**Concurrency Scenario:** 10,000 Customers, 2% Duplicate Rate, 30s Down OrderService

---

## 1. Traffic & Request Summary
| Metric | Value |
|---|---|
| Total HTTP Requests | 10,296 |
| Reserve Requests | 10,190 |
| Duplicate Requests Sent | 190 |
| Duplicate Replays Detected (200 OK) | 3 |
| Reservations Succeeded (201 Created) | 106 |
| SOLD_OUT Rejections (409 Conflict) | 9,894 |
| Payment Requests (/pay) | 106 |
| Payment Successes | 100 |
| Payment Failures | 6 |

---

## 2. Performance & Latency
| Metric | Result |
|---|---|
| Total Duration | 54.92 seconds |
| Throughput | 187.5 req/sec |
| Latency p50 | 59.00 ms |
| Latency p95 | 1049.04 ms |
| Latency p99 | 5017.92 ms |

---

## 3. Inventory State (Product X)
| Column | Final Value | Conservation Check |
|---|---|---|
| Available Quantity | 0 | Non-negative |
| Reserved Quantity | 0 | Non-negative |
| Sold Quantity | 100 | Non-negative |
| Total Initial Stock | 100 | 100 |
| **Sum (Avail + Res + Sold)** | **100** | **Invariant Holds: True** |

---

## 4. Orders, Outbox & Resiliency
| Metric | Value |
|---|---|
| Confirmed Reservations | 100 |
| Orders Created | 100 |
| Orders Stuck in Outbox (Pending) | 0 |
| Dead-Letter Events | 0 |
| Downstream Outage Simulated | 30.0s (at 40% run) |
| Circuit Breaker & Retry Recovery | Recovered all confirmed orders upon restoration |

---

## 5. Invariant Assertions
- [x] **Sold + Reserved <= 100**: `100 + 0 <= 100` -> **PASS**
- [x] **No negative quantities**: Available: 0, Reserved: 0, Sold: 100 -> **PASS**
- [x] **No duplicate payments**: Distinct Payments: 106, Total: 106 -> **PASS**
- [x] **No duplicate orders**: Distinct Orders: 100, Distinct Reservations: 100 -> **PASS**
- [x] **Every CONFIRMED reservation has exactly one order**: Confirmed Reservations: 100, Created Orders: 100 -> **PASS**

**Overall Invariant Verification Status: ALL ASSERTIONS PASSED [OK]**
