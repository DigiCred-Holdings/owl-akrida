# ACA-Py Basic Message Benchmark — Conclusions

**Environment:** clean worktree `/home/development/digicred/owl-akrida-benchmark` @ `e002b17` (`origin/main` + local benchmark harness)  
**Stack:** Docker Compose demo overlay (`issuer` ACA-Py 1.3.0 + Askar/Postgres + Redis, optional ACA-Py 0.6 mediator, Locust/Credo holders)  
**Host:** 12 CPUs, ~16 GB RAM (WSL2)  
**Workload:** exactly 10,000 end-to-end basic messages (`ping`, 4 bytes), counted only after Credo `BasicMessageStateChanged`  
**Issuer config for measured runs:** `no-ledger`, `log-level=info` (plus one debug comparison), wallet Postgres `max_connections` 5 vs 50  

Raw artifacts: `results/basicmsg/<profile>/` (Locust CSV/HTML, docker stats, postgres samples, logs).  
Summary table: [`SUMMARY.md`](./SUMMARY.md).

## Results (steady-state RPS excludes connection setup)

| Profile | Shape | Mediation | Steady RPS | msg p50 | Issuer CPU peak | Failures |
|---|---|---|---:|---:|---:|---:|
| direct-1x20 | 1×20 | no | 25.02 | 35 ms | 55% | 0 |
| direct-1x20-r2 | 1×20 | no | 23.79 | 36 ms | 58% | 0 |
| direct-20x1 | 20×1 | no | 46.87 | 410 ms | 149% | 0 |
| direct-20x1-r2 | 20×1 | no | 47.43 | 410 ms | 152% | 0 |
| direct-20x1-pool50 | 20×1 | no | **51.25** | 380 ms | 150% | 0 |
| direct-20x1-debug | 20×1 | no | 45.32 | 430 ms | 146% | 0 |
| mediated-1x20 | 1×20 | yes | 17.13 | 52 ms | 48% | 0 |
| mediated-20x1 | 20×1 | yes | 31.08 | 650 ms | 112% | 0 msg / setup errors |

## What we conclude

### 1. The ~45–50 msg/s ceiling is real for concurrent direct delivery
With 20 concurrent holders and default wallet pool=5, steady-state throughput was **~47 msg/s** (repeatable: 46.87 and 47.43). Raising the wallet pool to 50 reached **51.25 msg/s**. That matches the order of magnitude in the claim (45–50 msg/s after Postgres/pool tuning).

### 2. A 1-holder × 20-connection “serial replay” understates ACA-Py capacity
The serial shape held at only **~24–25 msg/s** with median latency ~35 ms and issuer CPU ~55%. That is largely the **load harness**: one Locust user sends messages one-by-one across its connections. It is useful as a lower bound / harness baseline, not as the ACA-Py concurrent ceiling.

### 3. Concurrency helps, then plateaus near one ACA-Py process
Going from 1→20 concurrent senders roughly **doubled** throughput (25 → 47 msg/s) while issuer CPU rose to **~1.5 cores (149–152%)**. Latency jumped (p50 35 ms → ~410 ms), which is consistent with queueing in a mostly single-threaded asyncio agent rather than free multi-core scaling.

This supports a **single-process / event-loop bound** interpretation more than a pure Postgres-pool interpretation:
- Default pool (5) already showed only ~6–7 Postgres sessions in samples.
- Pool=50 raised observed sessions (~21) and gained only ~+4 msg/s (47 → 51).
- Issuer CPU was already ~1.5 cores at the 47 msg/s plateau.

We do **not** claim we proved “the serialized event loop” via profiler traces. The evidence is performance-shaped: concurrent load stops scaling while the ACA-Py container is CPU-saturated around one primary process, and Postgres is not the dominant limiter.

### 4. Debug logging was a small effect here; mediation was large
- `info` → `debug` on the concurrent direct path: 47.43 → 45.32 msg/s (~4% drop). Not negligible, but not the main cliff.
- Mediator path cut throughput substantially: serial 25 → 17 msg/s; concurrent 47 → 31 msg/s. Any mediated production path will see mediator/pickup overhead on top of the issuer limit.

### 5. Mediated concurrent setup was flaky; message phase still completed
`mediated-20x1` completed all 10,000 messages with 0 message failures, but Locust exited non-zero due to **Credo/mediator startup** errors (12 setup failures). Treat 31 msg/s as a valid message-phase result with noisy warmup, not a perfectly clean run.

## Comparison to the original claim

| Claim element | Our finding |
|---|---|
| 10k basic messages / 20 connections | Reproduced (exact count, both 1×20 and 20×1) |
| ~45–50 msg/s after pool/ping tweaks | **Confirmed** for concurrent direct (~47 default pool, ~51 pool=50) |
| Bottleneck is “serialized event loop” | **Plausible and consistent** with CPU plateau + weak pool sensitivity; not profiler-proven |
| Removing ping / increasing pool is the unlock | Pool helps **slightly**; it does not move the ceiling by an order of magnitude. `auto-ping-connection` is a connection-setup concern and was left enabled; it was not the measured steady-state differentiator |

## Limitations
- Local Docker/WSL2, not a production-sized host or clustered ACA-Py.
- Issuer ran `no-ledger` for basic-message isolation; ledger-enabled deployments may differ.
- Holders are Credo/AFJ Node processes in the Locust container; harness CPU/RAM can contribute, especially at 20 holders.
- No Python/asyncio flame graphs or `asyncio` task introspection inside ACA-Py.
- Message payload was 4-byte `ping` (not a 1 KB body).

## Practical takeaway
For local ACA-Py 1.3.0 basic messages on this stack, expect roughly:
- **~25 msg/s** if the client is effectively serial,
- **~45–50 msg/s** under modest concurrency against one issuer process,
- **lower** once a mediator is in path,
- and only **marginal** gains from enlarging the Askar/Postgres pool once you are already near that plateau.

To go materially beyond ~50 msg/s you likely need horizontal scaling (multiple ACA-Py workers/instances), deeper event-loop/IO profiling, or architectural changes—not just Postgres pool tuning.
