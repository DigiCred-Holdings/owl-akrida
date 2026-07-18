# ACA-Py DIDComm Basic-Message Throughput — Investigation Report

**Status:** complete for the questions posed; open items listed in §9
**Harness:** Owl Akrida (`benchmark/basic-msg-10k` branch) + local benchmark overlay
**Issuer under test:** ACA-Py `py3.12-1.3.0` (Askar wallet); harness since re-based on
`py3.13-1.6.0` and re-verified (see §9 note)
**Host (reference runs):** 12 vCPU, ~16 GB RAM, Docker on WSL2
**Author:** Patrick St-Louis
**Replication guide:** [`REPLICATE_THROUGHPUT.md`](./REPLICATE_THROUGHPUT.md)
**Related docs:** [`throughput/CONCLUSIONS.md`](./throughput/CONCLUSIONS.md) · [`throughput/ISOLATION_CONCLUSIONS.md`](./throughput/ISOLATION_CONCLUSIONS.md) · [`throughput/FASTPATH.md`](./throughput/FASTPATH.md)

---

## 1. Executive summary

A stress test reported that a single ACA-Py agent could only push **~45–50 DIDComm basic
messages/second**, attributed to a "serialized event loop," while a modern Credo agent was
reported to reach **~170 msg/s**. We reproduced the ACA-Py number independently, isolated the
cause, built a fast-path plugin to address it, and measured the real ceiling.

**Headline findings:**

1. Stock single-process ACA-Py plateaus at **~48–55 msg/s** on basic-message send — reproduced.
2. The limiter is **not** storage, the mediator, the holder, or the DIDComm cryptography. It is
   **per-send overhead** in ACA-Py's send pipeline (admin route, per-message `ConnRecord`
   fetch, per-message Askar session, the outbound queue/encode state machine, and repeated key
   fetch/conversions).
3. A fast-path plugin (`didcomm_fastpath`) that caches per-connection send material — **without
   changing the wire format or the crypto** — roughly **doubled** throughput to **~89 msg/s** at
   **half** the CPU when sending to real Credo holders.
4. That ~89 was a **test-rig artifact**, not the issuer's ceiling: co-located Credo holders
   consumed 11–12 of the 12 host cores while the issuer used only ~1–1.5.
5. With a lightweight mock recipient, one ACA-Py fast-path process reached **~170–210 msg/s**
   (peak ~207 at 60 concurrent connections) — **matching the Credo "~170 msg/s" figure**.

---

## 2. Problem statement

> "I did some stress testing on the ACA-Py agent; the results are worse than expected. For testing
> I made 10k basic messages across 20 connections and replayed them, so the bottleneck is not the
> stressing agent. The biggest issue is the serialized event loop. I tuned Postgres, removed ping,
> and increased the pool to find the limit, and could only reach 45–50 msg/s. The latest Credo
> module is able to process 170 messages per second."

Two questions followed from this:

- **Q1.** Can we independently reproduce the ~45–50 msg/s ceiling and identify its true cause?
- **Q2.** Can we raise it, and what is the actual ceiling of a single ACA-Py process?

---

## 3. Test environment

| Component | Version / spec |
|---|---|
| Host | 12 vCPU, ~16 GB RAM, Docker on WSL2 |
| Issuer (system under test) | ACA-Py `ghcr.io/openwallet-foundation/acapy-agent:py3.12-1.3.0`, Askar wallet |
| Wallet storage | PostgreSQL 14.3 (`postgres_storage`), and Askar SQLite for comparison |
| Cache plugin | `acapy-cache-redis` (Redis 7 alpine), toggled on/off |
| Holders (load) | Credo / Aries Framework JS **0.5.17** Node agents, in-memory SQLite, one process per connection |
| Load driver | Locust (gevent), exact-count harness |
| Mediator (optional) | ACA-Py 0.6-based mediator, removed for isolation runs |
| Profiler | `py-spy` 0.4.1 (native sampling) |

**Workload:** basic messages with a 4-byte `ping` payload. Runs use an **exact message counter**
and report **steady-state RPS** (excludes connection-setup warmup).

**Two measurement modes:**

- `e2e` — issuer send **and** wait for the Credo holder's `BasicMessageStateChanged` receipt.
- `admin_only` — only the ACA-Py send call; holder receipt not awaited at the driver
  (note: the fast-path still awaits the recipient's HTTP `200`, see §7).

---

## 4. Components under test

```
                 ┌────────────┐   HTTP admin API    ┌──────────────────────────┐
   Locust  ─────▶│  Load-gen  │────────────────────▶│   ACA-Py issuer (SUT)    │
 (exact-count)   │  container │  POST send-message   │  Askar + Postgres wallet │
                 │  N Credo   │◀────────────────────│  DIDComm v1 pack + POST   │
                 │  holders   │   DIDComm delivery    └──────────────────────────┘
                 └────────────┘   (HTTP, packed)                 │
                                                                 ▼
                                          real holder  ─OR─  mock-holder sink (§7)
```

- **Connection shape `20×1`** = 20 Locust users, one connection each (20 concurrent senders).
  This is the concurrent shape that stresses the issuer; `1×20` (one serial user) understates it.

---

## 5. Methodology

The investigation proceeded in four phases, each narrowing the cause:

1. **Reproduction** — full stack (issuer + Postgres + Redis + mediator), 10k messages, shapes
   `1×20` and `20×1`, pool 5 vs 50, `info` vs `debug`, direct vs mediated.
2. **Isolation** — remove mediator, remove Redis, `no-ledger`; compare Postgres vs SQLite and
   `e2e` vs `admin_only`; size the Askar pool above concurrency so it is not an artificial limit.
3. **Root-cause + fix** — `py-spy` the send path, then build `didcomm_fastpath` to remove the
   per-send overhead while keeping the DIDComm v1 wire format identical.
4. **Ceiling** — a concurrency sweep with host-wide CPU sampling, then a **mock-holder sink** to
   remove recipient cost from the critical path and measure the issuer's true capacity.

---

## 6. Results

### 6.1 Reproduction (full stack, 10k messages)

| Profile | Shape | Mediation | Steady RPS | Issuer CPU peak |
|---|---|---|---:|---:|
| direct-1x20 | 1×20 (serial) | no | 25.0 | 55% |
| direct-20x1 | 20×1 (concurrent) | no | 46.9 | 149% |
| direct-20x1-pool50 | 20×1, wallet pool 50 | no | 51.3 | 150% |
| mediated-20x1 | 20×1 | yes | 31.1 | 112% |

Concurrency ~doubled throughput (25 → 47) while issuer CPU hit ~1.5 cores; enlarging the wallet
pool 5→50 added only ~4 msg/s. **~45–50 msg/s confirmed.**

### 6.2 Isolation (mediator/Redis removed, no-ledger, 20×1, pool ≥ concurrency)

| Profile | Storage | Measure | Steady RPS | Issuer CPU peak |
|---|---|---|---:|---:|
| isolate-pg-e2e | Postgres | e2e | 50.3 | 162% |
| isolate-sqlite-e2e | SQLite | e2e | 55.2 | 155% |
| isolate-pg-admin | Postgres | admin-only | 48.3 | 155% |
| isolate-sqlite-admin | SQLite | admin-only | 47.7 | 163% |

- Postgres ≈ SQLite (~10% apart) → **storage is not the ceiling**.
- admin-only ≈ e2e → **holder/receipt path is not the steady-state limiter**.
- CPU pinned at ~1.5 cores in every variant → **single-process CPU/event-loop bound**.
- Side finding: with Redis off and the default pool (5), connection **setup** collapsed
  (pool timeouts). Redis was masking an undersized pool for setup, not lifting the message ceiling.

### 6.3 Root cause (py-spy)

The stock pack (`aca-py` Askar DIDComm v1) already offloads crypto to a thread executor. Sampling
showed the **pack worker was only ~13%** of process-active time; the bulk was Askar
session/FFI lifecycle (~45%), admin routes/middleware (~12–18%), and outbound HTTP (~7%). i.e.
**the crypto was not the bottleneck — the per-send scaffolding was.**

### 6.4 The fix, against real Credo holders

| Path | Steady RPS | Issuer CPU peak |
|---|---:|---:|
| Stock `/connections/{id}/send-message` | 48.3 | 155% |
| `didcomm_fastpath`, send only | 89.5 | 80% |
| `didcomm_fastpath`, end-to-end (holder receipt awaited) | 89.0 | ~80% |

**~1.85× at ~half the CPU.** e2e ≈ send-only proves the holders actually received and validated
every message, so the cached-material envelopes are wire-valid.

### 6.5 Concurrency sweep — the ~89 was the rig, not the issuer

| Holders | Steady RPS | Issuer CPU | Host CPU | Load-gen (holders) CPU |
|---:|---:|---:|---:|---:|
| 20 | 89.5 | ~0.8 core | — | — |
| 40 | 89.7 | ~0.7–0.9 core | — | — |
| 60 | 84.0 | ~1.0–1.5 core | **99.5% (all 12 cores)** | **~1,100–1,250% (11–12 cores)** |

Throughput went flat then declined while **per-message latency doubled** — saturation. Host-wide
sampling revealed the **Credo holders ate 11–12 cores**; the issuer was **starved**, not maxed.

### 6.6 True ceiling — mock-holder sink

Recipient replaced by an aiohttp sink that returns `200` without unpacking; packing still uses the
**real** connection keys.

| Holders | Steady RPS | Pack mean | Deliver mean | Total mean | Sink received |
|---:|---:|---:|---:|---:|---:|
| 20 | **169** | 16.3 ms | 12.8 ms | 30.7 ms | 2000 |
| 40 | **202** | 25.9 ms | 22.3 ms | 56.1 ms | 2000 |
| 60 | **207** | 35.4 ms | 36.0 ms | 79.7 ms | 2000 |
| 80 | 195 | 43.4 ms | 48.4 ms | 101.6 ms | 3000 |

**A single ACA-Py fast-path process clears ~170–210 msg/s**, peaking ~207 at 60 concurrency —
matching the reported Credo figure once the recipient is cheap.

---

## 7. What the `didcomm_fastpath` plugin changes

A dedicated admin route, `POST /didcomm-fastpath/connections/{conn_id}/send-message`, that
produces the **same DIDComm v1 wire format** as the stock route (fresh CEK, fresh nonces, fresh
AEAD per message) but removes the repeated per-send work:

**Cached once per connection** (on first send): endpoint, recipient/routing verkeys, the sender
signing key (fetched from Askar once), the Ed25519→X25519 conversions, and the reusable
sealed-sender blob.

**Per subsequent send:** build message JSON (no marshmallow) → pack in a bounded thread pool
(`FASTPATH_PACK_WORKERS`, default 32) → POST over a persistent keep-alive `aiohttp` session. No
`ConnRecord` fetch, no per-send profile session, no outbound queue round-trip.

Observability: `GET /didcomm-fastpath/stats` (per-stage timings + observed RPS);
`DELETE /didcomm-fastpath/stats`; `DELETE /didcomm-fastpath/cache`.

Benchmark-only aids: `FASTPATH_DELIVER_OVERRIDE` redirects the delivery HTTP hop to the mock sink
(keys/pack unchanged) so recipient cost can be excluded from measurement.

> The plugin is also committed to `digicred-crms` (branch `feat/didcomm-fastpath-plugin`) in the
> repo's plugin conventions (poetry, `definition.py`, `v1_0/`), with tenant-auth on all routes.

---

## 8. What we know for sure

1. **Stock single-process ACA-Py 1.3.0 does ~48–55 msg/s** on basic-message send in this
   environment; CPU sits at ~1.5 cores. (Reproduced across ≥6 runs.)
2. **Storage is not the bottleneck** — Postgres vs SQLite differ ~10% once the pool is adequate.
3. **The mediator and the holder-receipt path are not the steady-state limiter** — admin-only ≈
   e2e; removing the mediator did not lift the ceiling.
4. **The DIDComm cryptography is not the bottleneck** — the fast path uses identical crypto and
   still nearly doubled throughput; py-spy put pack at ~13% of active time.
5. **The real limiter is per-send pipeline overhead** — connection/record lookups, per-send Askar
   sessions, the outbound queue/encode machinery, and repeated key fetch/conversions.
6. **Caching that overhead ~doubles throughput at half the CPU** and keeps messages wire-valid
   (proven by Credo holders receiving them e2e).
7. **A single fast-path ACA-Py process reaches ~170–210 msg/s** when the recipient is cheap —
   the same order as the reported Credo number.
8. **The load generator must be accounted for.** Co-located Credo holders can consume the entire
   host and cap measured throughput independently of the issuer.

---

## 9. What we do NOT know (open questions & caveats)

1. **The absolute issuer ceiling on dedicated hardware.** Even in the sink runs the load
   generator was co-located and briefly drove the host to ~99%. ~207 msg/s may still be partly
   rig-limited; a **remote load generator or CPU-pinned issuer** is needed for a clean number.
2. **Why 80 holders regressed** (195 vs 207). Likely host CPU contention from idle-but-warm Credo
   processes, but not isolated.
3. **Production-safety of the plugin is partially addressed.** Cache invalidation is now
   implemented (event-bus eviction on any `connections` record event — update, DID rotation,
   deletion — plus a `FASTPATH_CACHE_TTL` fallback, default 300 s) and verified against a live
   delete. Still open: BasicMessage record persistence and send-side webhooks; load-tested
   mediator forward-wrapping (implemented, untested); multi-recipient packing.
4. **Real-recipient throughput at scale.** Production recipients (mobile wallets via a mediator)
   have a very different profile than local Credo holders; our numbers bound the **issuer**, not a
   full mediated delivery path.
5. **Horizontal scaling linearity is a hypothesis**, not measured. We expect ~N×200 with N
   replicas on shared storage, but did not run a multi-replica test.
6. **The Credo "170 msg/s" claim's exact conditions are unknown** (version, concurrency, recipient
   type, whether `processDidCommMessagesConcurrently` was set). Our holders were Credo 0.5.17;
   current Credo is 0.7.0. We matched the number but not necessarily the setup.
7. **py-spy native sampling had reliability warnings**; the crypto-vs-FFI split in §6.3 is
   approximate, not exact.
8. **Payload size effect** — all runs used a 4-byte payload; larger bodies were not tested.

> **ACA-Py 1.6.0 note.** After the report's reference runs, the harness base image was bumped to
> `py3.13-1.6.0` (1.6.0 ships on Python 3.13; there is no py3.12 tag). All plugin internals the
> fast path relies on are unchanged, and a 10k fastpath e2e re-run on 1.6.0 completed cleanly
> (~111 msg/s steady-state with 20 real Credo holders on this host — same order as the 1.3.0
> numbers; run-to-run host load explains the delta). The reference tables above were **not**
> re-measured on 1.6.0.

---

## 10. Reproduction

All commands run from the **repository root** (this branch). See
[`REPLICATE_THROUGHPUT.md`](./REPLICATE_THROUGHPUT.md) for the short form.

### 10.1 Prerequisites

- Docker + Docker Compose v2
- Free host RAM scales with holder count (each Credo holder ≈ 150–200 MB). Budget accordingly;
  this 12-core/16 GB host is comfortable to ~40–60 holders.

### 10.2 One-time: bring the stack up

```bash
bash scripts/run-basicmsg-benchmark.sh up
```

### 10.3 Reproduce the stock ceiling (isolation matrix)

```bash
# mediator/Redis removed, no-ledger, 20×1, pool sized above concurrency
TARGET_MESSAGE_COUNT_OVERRIDE=2000 bash scripts/run-basicmsg-benchmark.sh isolate
# → results/basicmsg/isolate-*/  and  results/basicmsg/ISOLATION.md
```

Expected: ~48–55 msg/s, issuer CPU ~1.5 cores, Postgres ≈ SQLite, admin ≈ e2e.

### 10.4 Reproduce the fast-path vs real Credo holders

```bash
TARGET_MESSAGE_COUNT_OVERRIDE=2000 bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin
TARGET_MESSAGE_COUNT_OVERRIDE=2000 bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-e2e
```

Expected: ~89 msg/s, issuer CPU ~0.8 core, e2e ≈ admin.

### 10.5 Reproduce the concurrency sweep (shows rig saturation)

```bash
for n in 20 40 60; do
  TARGET_MESSAGE_COUNT_OVERRIDE=2000 LOCUST_USERS_OVERRIDE=$n \
    bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin-$n
done
```

Expected: throughput flat/declining while the host saturates (Credo holders dominate CPU).

### 10.6 Reproduce the true ceiling (mock-holder sink)

```bash
for n in 20 40 60 80; do
  TARGET_MESSAGE_COUNT_OVERRIDE=2000 LOCUST_USERS_OVERRIDE=$n \
    bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin-sink-$n
done
# Per-run: results/basicmsg/fastpath-pg-admin-sink-$n/{fastpath-stats.json,mock-holder-stats.json}
```

Expected: ~170 msg/s at 20, peaking ~207 at 60.

### 10.7 Inspect fast-path per-stage timings live

```bash
curl -s localhost:8150/didcomm-fastpath/stats | python3 -m json.tool
curl -s localhost:8090/stats                   # mock-holder counters (sink runs)
```

### 10.8 Tear down

```bash
bash scripts/run-basicmsg-benchmark.sh down     # keep volumes
bash scripts/run-basicmsg-benchmark.sh reset    # remove volumes
```

---

## 11. Recommendations / next steps

**To establish the honest single-process ceiling:**
- Run the load generator on a **separate host**, or pin the issuer to dedicated cores
  (`cpuset`), then repeat §10.6. This removes the last rig confound.

**To scale in production:**
- Treat **~200 msg/s per fast-path process** as the working per-process budget and **scale
  horizontally** behind shared Postgres; validate linearity with 2–4 replicas.
- Keep Redis for connection-setup pool pressure; it does not lift the steady-state ceiling.

**To productionize `didcomm_fastpath`:**
- ~~Add cache invalidation on DID rotation / connection deletion (hook or TTL).~~ **Done** —
  event-bus eviction on `connections` record events + `FASTPATH_CACHE_TTL` (default 300 s).
- Restore send-side BasicMessage persistence + webhook if consumers depend on them.
- Load-test mediator forward-wrapping and multi-recipient packing.

**Deeper single-process gains (only if needed after the above):**
- Prototype an ECDH shared-secret / key-conversion cache inside pack.
- Tune `FASTPATH_PACK_WORKERS`; confirm the native crypto releases the GIL.

---

## 12. Artifact index

| Path | Contents |
|---|---|
| `docs/ACAPY_THROUGHPUT_REPORT.md` | This report |
| `docs/REPLICATE_THROUGHPUT.md` | Short build / run guide |
| `docs/throughput/` | Phase write-ups (conclusions, isolation, fastpath, summary) |
| `results/basicmsg/<profile>/` | Raw per-run Locust CSV/HTML, docker stats, PG samples, logs (gitignored) |
| `instance-configs/acapy-agent/plugins/didcomm_fastpath/` | The plugin (benchmark copy) |
| `mock-holder/` | Lightweight HTTP sink recipient |
| `scripts/run-basicmsg-benchmark.sh` | Orchestrator |
| `scripts/collect-stats.sh` | Docker/PG stats collector |

> **Measurement note:** `collect-stats.sh` samples only compose-managed services; the ephemeral
> `compose run` load-agent (the Credo holders) is **not** captured. Host-wide CPU sampling was
> required to reveal the load-generator saturation in §6.5. Future runs should capture host CPU
> and the load-gen container explicitly.
