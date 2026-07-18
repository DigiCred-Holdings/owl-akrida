# ACA-Py Isolation — Does storage (Postgres vs SQLite) matter?

**Goal:** strip everything except ACA-Py and see what actually sets the ~50 msg/s ceiling.  
**Setup:** mediator removed, Redis cache plugin removed, `no-ledger`, concurrent 20×1, 2,000 messages per run, Postgres pool sized above concurrency (30) so it is not an artificial limiter.  
**Two measurements:**
- `e2e` — issuer send + wait for Credo `BasicMessageStateChanged` (full path)
- `admin_only` — only the ACA-Py `/connections/{id}/send-message` admin call (holder receipt not awaited)

## Results

| Profile | Storage | Measure | Steady RPS | Issuer CPU peak |
|---|---|---|---:|---:|
| isolate-pg-e2e | Postgres | e2e | 50.3 | 162% |
| isolate-sqlite-e2e | SQLite | e2e | 55.2 | 155% |
| isolate-pg-admin | Postgres | admin-only | 48.3 | 155% |
| isolate-sqlite-admin | SQLite | admin-only | 47.7 | 163% |
| direct-20x1 (baseline, Redis on) | Postgres | e2e | 46.9 | 149% |

## Answers

### Would removing Postgres for SQLite help?
**Only marginally — about +10% (50 → 55 msg/s), not a step change.** Storage is not the ceiling. SQLite even used a single local file with no network hop and still landed in the same ~50 msg/s band.

Practically, SQLite is also **not** a good production choice here: it is a single-writer local file, so it does not scale out and removes the ability to run multiple ACA-Py replicas against shared storage. The ~10% is not worth losing horizontal scalability.

### Where is the ceiling, then? — ACA-Py's own CPU-bound processing
Three independent signals all point at the ACA-Py process itself:

1. **CPU is pinned at ~1.5 cores (155–163%) in every variant**, regardless of storage backend or whether we waited for the holder. That is the classic single-process asyncio + native-crypto ceiling.
2. **admin-only ≈ e2e** (48 vs 50 for PG). Removing the Credo holder receipt from the measured path barely changed throughput — so the **holder/receipt side is not the limiter**; the cost is on ACA-Py accepting and processing the send (DIDComm pack/encrypt + outbound).
3. **Postgres ≈ SQLite** (50 vs 55). Swapping the storage engine barely moved it — so the **wallet/DB is not the limiter** either, once the pool is adequate.

### Bonus finding: the Redis cache was hiding wallet-pool pressure
With Redis **off** and the default pool=5, connection setup collapsed ("pool timed out, acquired_after_secs≈24"). The Redis cache plugin was absorbing wallet lookups. So Redis matters for **connection-setup pool pressure**, not for lifting the steady-state message ceiling (baseline with Redis was 46.9, isolated without Redis but adequate pool was 50.3).

## Bottom line
The bottleneck is **ACA-Py itself — a single ~1.5-core-bound process on the DIDComm send path**, not Postgres, not the holder, not the mediator. Removing Postgres in favor of SQLite yields only ~10% and costs you scalability.

To materially exceed ~50 msg/s: **run multiple ACA-Py instances/workers behind shared storage** (horizontal scale), and/or profile the send path (`py-spy`) to see how much is DIDComm crypto vs Python event-loop overhead. Storage tuning is a dead end past this point.

Artifacts: `results/basicmsg/isolate-*/` and [`ISOLATION.md`](./ISOLATION.md).
