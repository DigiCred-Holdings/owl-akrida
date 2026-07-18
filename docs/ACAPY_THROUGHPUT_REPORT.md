# ACA-Py DIDComm Basic-Message Throughput — Investigation Report

**Issuer under test:** ACA-Py `py3.13-1.6.0` (Askar wallet)
**Harness:** Owl Akrida (`benchmark/basic-msg-10k` branch) + local benchmark overlay
**Host:** 12 vCPU, ~16 GB RAM, Docker on WSL2
**Author:** Patrick St-Louis
**Related:** [`REPLICATE_THROUGHPUT.md`](./REPLICATE_THROUGHPUT.md) · [`throughput/FASTPATH.md`](./throughput/FASTPATH.md)

> All numbers in this report were measured on **ACA-Py 1.6.0** (10k messages per run, 20×1
> concurrent shape, isolated stack — no mediator/Redis/ledger, Askar pool sized above concurrency).

---

## 1. The claim we investigated

A stress test reported that a single ACA-Py agent could push only **~45–50 DIDComm basic
messages/second**, blamed it on a **"serialized event loop,"** and noted a modern Credo agent
reportedly reaching **~170 msg/s**. We set out to (1) reproduce it, (2) find the real cause, and
(3) see how far one ACA-Py process can actually go.

## 2. Answers, up front

| The claim | What we found on 1.6.0 |
|---|---|
| "~45–50 msg/s per agent" | Stock 1.6.0 does **~58–70 msg/s** — a real, low ceiling, but 1.6.0 is faster than the 1.3.0 that produced ~48. |
| "serialized event loop is the issue" | Partly. The event loop is ~27% of the hot path; the **bigger cost is per-send Askar session/FFI churn (~42%)**. The DIDComm **crypto is not the bottleneck**. |
| "Credo does ~170 msg/s" | A single ACA-Py **fast-path** process reaches **~242 msg/s** with a cheap recipient — above the cited Credo number. |

**Bottom line:** the limiter is **per-send pipeline overhead**, not storage, the mediator, the
holder, or cryptography. Caching that overhead (`didcomm_fastpath`, identical wire format) lifts a
single process from ~70 to ~94 msg/s (admin) / ~105 (e2e) against real holders, and to ~242 when
the recipient is cheap — with **lower CPU per message**.

---

## 3. Reproduction & isolation (stock 1.6.0)

20 concurrent senders (20×1), isolated stack, Askar pool 30, 4-byte `ping` payload.

| Profile | Storage | Measure | Steady msg/s | Issuer CPU (mean/peak) |
|---|---|---|---:|---:|
| `isolate-pg-admin` | Postgres | admin send | **70.0** | 124% / 146% |
| `isolate-pg-e2e` | Postgres | e2e (receipt) | 62.2 | 128% / 164% |
| `isolate-sqlite-admin` | SQLite | admin send | 61.3 | 128% / 149% |
| `isolate-sqlite-e2e` | SQLite | e2e (receipt) | 58.1 | 130% / 147% |

- **Storage is not the ceiling** — Postgres ≈ SQLite (within ~15%).
- **The holder/receipt path is not the steady-state limiter** — admin ≈ e2e (within ~12%).
- **Single-process CPU bound** — issuer pinned at ~1.3 cores in every variant.
- (Setup-only note: with the default Askar pool of 5, connection *setup* collapses under
  concurrency; Redis masked that. It affects setup, not the message ceiling.)

## 4. Root cause — where the time goes (py-spy, 1.6.0)

Sampling the issuer's main thread under load (directional — native unwinding drops ~40% of
samples):

| Area | Self time |
|---|---:|
| Askar session / FFI lifecycle (`invoke_async`, `invoke`, `invoke_dtor`) | **~42%** |
| asyncio event loop | ~27% |
| Outbound HTTP write | ~9% |
| Admin route / middleware / serialization | ~3% |
| DIDComm crypto (pack) | negligible on the main thread (runs in a thread pool) |

The crypto is already offloaded and cheap; the dominant cost is **opening an Askar session and
crossing the FFI boundary on every send**, plus event-loop churn. That is what a fast path can
remove without touching the wire format.

## 5. The fix — `didcomm_fastpath`

Same DIDComm v1 wire format (fresh CEK/nonce/AEAD per message), but per-connection send material
is **cached after the first send**: endpoint, recipient/routing verkeys, the sender key, the
Ed25519→X25519 conversions, and the sealed-sender blob. Per subsequent send: build JSON (no
marshmallow) → pack in a thread pool → POST over a keep-alive session. No `ConnRecord` fetch, no
per-send Askar session, no outbound-queue round-trip.

**Against real Credo holders (1.6.0):**

| Path | Steady msg/s | Issuer CPU (mean) |
|---|---:|---:|
| Stock admin send | 70.0 | 124% |
| `didcomm_fastpath` admin send | **94.4** | 106% |
| `didcomm_fastpath` e2e (receipt) | **104.9** | — |

~1.35× (admin) / ~1.5× (e2e) more throughput at **lower CPU per message**, wire-valid (holders
received and validated every message e2e).

> These figures are **bounded by local host resources, not by ACA-Py.** The issuer sits at ~1 core
> while the co-located Credo holders saturate the box (they do ~10× the issuer's CPU work per
> message), so ~94–105 is the load generator's limit here — see §6 for the issuer's true ceiling.

## 6. How far one process really goes — mock-holder sink

Real-holder numbers are **rig-limited**: co-located Credo holders do ~10× the issuer's CPU work
(full unpack per message). Replacing the recipient with a lightweight sink that returns `200`
without unpacking (packing still uses the real connection keys) reveals the issuer's true ceiling:

| Connections | Steady msg/s | Issuer CPU (mean) |
|---:|---:|---:|
| 20 | 220.6 | — |
| 40 | 227.1 | — |
| 60 | **241.9** | ~81% |

A single ACA-Py fast-path process clears **~220–242 msg/s** (peak at 60 connections) at under one
core — so the ~94–105 seen against real holders is the load generator's limit, not ACA-Py's.

---

## 7. Cache TTL sweep

To reduce how long the cached sender private-key handle remains reachable, we swept the absolute
TTL on the real-Credo admin path (10k messages, 20 connections):

| TTL | Steady msg/s | Cold resolves | p50 / p95 | Total mean |
|---:|---:|---:|---:|---:|
| 300 s | 93.83 | 20 | 200 / 310 ms | 88.69 ms |
| 60 s | **94.72** | 40 | 200 / 290 ms | 86.85 ms |
| 30 s | 92.77 | 80 | 200 / 280 ms | 88.37 ms |
| 10 s | 93.67 | 220 | 200 / 280 ms | 88.13 ms |

All runs had zero failures. Throughput stayed within a ~2% band, so **10-second refreshes caused
no measurable regression** in this test despite 11× more cold resolves than the 300-second run.

Default is now **30 seconds** (the sweep shows anything in this range is performance-safe;
30 s keeps connections warm across multi-step flows) with **active ~1s background eviction**
(plus lazy check on access), cache keys scoped as `(local_tenant_wallet_id, connection_id)` —
where `wallet_id` is *our* ACA-Py tenant subwallet, not the remote peer — and an LRU cap
(`FASTPATH_CACHE_MAX`, default 8192; size by peak concurrent active connections, not total).
Idle entries no longer linger past the TTL.

### Post-hardening verification (TTL=30, MAX=8192)

After adding tenant-scoped keys, active TTL, LRU bounds, key-handle disposal, and the
wallet-removal hook, the full 10k matrix was re-run with the new defaults — **no regression**:

| Path | Steady msg/s | Failures | Cache signal |
|---|---:|---:|---|
| Fast-path e2e (real Credo, 20 conns) | **102.3** | 0 | `evictions_lru=0`, `expirations_ttl` fired on idle |
| Fast-path mock sink (20 conns) | **227.3** | 0 | drained to 0 entries after idle via active sweep |

Both reproduce §5/§6 within noise, confirming the hardening (tenant isolation + bounded,
self-draining cache) costs nothing in throughput. `cache_entries` stayed at 20 (≪ 8192, so LRU
never triggered), and after traffic stopped the background sweeper reclaimed every idle entry and
its `sender_xk` within the 30 s TTL — the intended behavior for bursty/idle holders.

### Kanon storage backend (TTL=30, MAX=8192)

The prior numbers use stock Askar. To check whether the fast-path ceiling depends on the storage
backend, the same 10k matrix was re-run against **`kanon_storage`** (SQLAlchemy 2.0 + asyncpg,
`wallet-type: kanon-storage-anoncreds`) instead of Askar — same base image (ACA-Py `py3.13-1.6.0`),
same fast-path plugin, same Postgres:

| Path | Askar | Kanon | Δ | Failures |
|---|---:|---:|---:|---:|
| Fast-path mock sink (issuer ceiling, 20 conns) | 227.3 | **204.4** | −10% | 0 |
| Fast-path e2e (real Credo, 20 conns) | 102.3 | **97.1** | −5% | 0 |

Kanon holds fast-path throughput within a small margin of Askar (the e2e path is still bounded by
the co-located Credo holders, not storage). Cache behaviour was identical — `cache_entries=20`,
`evictions_lru=0`, and the active TTL sweep drained idle entries (`expirations_ttl` fired) — and
schema bootstrap (`auto_migrate`, 5 tables) plus fast-path resolve/pack worked unchanged over the
Kanon-backed wallet. Reproduce with the `kanon-fastpath-admin-sink` / `kanon-fastpath-e2e`
profiles (Dockerfile `docker/Dockerfile.kanon`).

---

## 8. Horizontal scaling (shared Postgres, mock-sink)

To check whether the per-process ceiling is additive, we ran the fast-path mock-sink workload
across **N = 1, 2, 3 issuer replicas** that share one Askar Postgres wallet. Each Locust user is
pinned (sticky) to one replica so connection setup and sends stay on the same process; each
replica has its own DIDComm inbound endpoint and its own per-process fast-path cache. Offered
load is held constant at **20 Locust users per replica**.

| Replicas | Users | Aggregate msg/s | Per-replica msg/s | vs 1× |
|---:|---:|---:|---:|---:|
| 1 | 20 | **185.3** | 185.3 | 1.00× |
| 2 | 40 | **250.7** | 125.4 | 1.35× |
| 3 | 60 | 243.4 | 81.1 | 1.31× |

- **Shared-wallet + sticky routing works.** Sends split evenly across replicas (N=2 ≈
  5019 / 4981; N=3 ≈ 3349 / 3321 / 3330) and every message landed at the mock sink (0 failures).
- **Aggregate peaks ~250 msg/s on this host, then plateaus.** Locust itself warned
  `CPU usage above 90%` during N=2 and N=3 — the load generator (and its Credo agents used for
  connection setup) saturated the 12-core box. Mean per-send latency on each issuer actually
  *dropped* at N=3 (≈19 ms vs ≈28 ms at N=1), so the issuers were under-loaded; the host was
  the limiter, not Postgres.
- **What this proves on one box:** multi-replica shared-wallet send is correct and adds
  capacity until the host is full. It does **not** prove linear N× scaling — that needs the
  load generator (and ideally the replicas) on separate hosts. Reproduce with
  `bash scripts/run-basicmsg-benchmark.sh scale-sweep 20` (writes `results/basicmsg/SCALING.md`).

---

## 9. What we know vs. don't

**Know (measured on 1.6.0):** the ~58–70 stock ceiling and its cause (per-send Askar/FFI + event
loop, not storage/mediator/crypto); the fast path's ~1.35–1.5× lift at lower CPU/msg with an
identical wire format; a single-process ceiling of ~242 msg/s with a cheap recipient; the fast
path holds within ~5–10% on the Kanon (SQLAlchemy) storage backend (§7); multi-replica shared-
wallet send works and lifts aggregate throughput until the host saturates (§8); the shared
bidirectional cache lifts the full inbound pipeline ~1.2× (174.7 → 210.5 handled msg/s) with hot
unpack at 0.8 ms vs 53 ms cold (Appendix A).

**Don't (open):**
- **Absolute ceiling on dedicated hardware** — even sink runs co-locate the load generator; a
  remote generator or CPU-pinned issuer would give a cleaner number.
- **py-spy split is directional** — ~40% of samples dropped to native unwinding; treat the
  percentages as shares, not exact.
- **True multi-host horizontal linearity** — §8 is single-host and host-CPU-limited past N=2;
  N×~240 behind shared Postgres across machines is still unmeasured.
- **Real mediated wallets** differ from local Credo holders; our numbers bound the *issuer*.
- **Remaining plugin gaps** — send-side BasicMessage persistence/webhooks and mediator
  forward-wrapping remain untested. Cache isolation, active TTL, LRU, wallet-removal hook,
  and Key disposal are in place and load-verified (§7).

---

## 10. Recommendations

- **Ceiling:** treat **~200–240 msg/s per fast-path process** as the working per-process budget;
  **scale horizontally** behind shared Postgres. On one host expect the aggregate to plateau
  once the load generator / Credo agents fill the cores (§8); true N× needs separate hosts.
- **Storage tuning is a dead end** past an adequate Askar pool; keep Redis only for setup-time
  pool pressure.
- **For a clean absolute / scaling number:** rerun §6 and §8 with the load generator on a
  separate host (or `cpuset`-pin the issuer replicas).
- **Key retention:** default **30-second active TTL** + LRU + tenant-scoped keys; the TTL
  sweep found no measurable throughput or latency penalty even at 10 seconds, so tighten
  the TTL freely if a shorter secret-retention window is required.
- **Inbound:** leave `FASTPATH_INBOUND=1` for busy receive paths (~1.2× on the full
  pipeline, Appendix A), but treat the `find_inbound_connection` override as version-sensitive —
  re-verify it (or set `FASTPATH_INBOUND=0`) when upgrading ACA-Py.
- **Deeper single-process gains (only if needed):** ECDH shared-secret cache inside pack; tune
  `FASTPATH_PACK_WORKERS`.

---

## 11. Reproduce

From the repository root (see [`REPLICATE_THROUGHPUT.md`](./REPLICATE_THROUGHPUT.md) for detail):

```bash
# stock isolation matrix (§3)  — expect ~58–70 msg/s, ~1.3 cores, PG≈SQLite, admin≈e2e
for p in isolate-pg-admin isolate-pg-e2e isolate-sqlite-admin isolate-sqlite-e2e; do
  bash scripts/run-basicmsg-benchmark.sh run $p
done

# fast path vs real Credo (§5) — expect ~94 admin / ~105 e2e
bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin
bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-e2e

# true ceiling via mock sink (§6) — expect ~220–242, peak at 60
for n in 20 40 60; do
  LOCUST_USERS_OVERRIDE=$n bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin-sink-$n
done

# Kanon storage backend (§7) — expect ~204 sink / ~97 e2e, on par with Askar
bash scripts/run-basicmsg-benchmark.sh run kanon-fastpath-admin-sink
bash scripts/run-basicmsg-benchmark.sh run kanon-fastpath-e2e

# horizontal scaling (§8) — N=1,2,3 replicas, shared PG, mock-sink
# on a 12-core host expect ~185 → ~250 → plateau (host CPU, not Postgres)
bash scripts/run-basicmsg-benchmark.sh scale-sweep 20

# inbound replay (Appendix A) — expect ~175 stock / ~210 fast-path handled msg/s
# (start the issuer with FASTPATH_INBOUND=0 for the stock baseline)
docker run --rm --entrypoint python --network owl-benchmark_app-network \
  -v "$PWD/scripts:/bench:ro" acapy-cache-redis \
  /bench/run-inbound-benchmark.py --messages 10000 --concurrency 20

# live per-stage timings
curl -s localhost:8150/didcomm-fastpath/stats | python3 -m json.tool
```

> **First-boot note:** on a fresh Postgres volume the DB can take longer to initialize than the
> issuer's store-open retry window (`ProfileError: Failed to open or provision store`). If the
> first scripted run fails, start the DB once (`… up -d issuer-db`), wait for `pg_isready`, rerun.

The issuer image is ACA-Py `py3.13-1.6.0` + `didcomm_fastpath` (built from
`instance-configs/acapy-agent/`); the `didcomm_fastpath` plugin also ships in `digicred-crms`.
Raw per-run artifacts (Locust CSV/HTML, docker stats, logs) land in `results/basicmsg/<profile>/`
(gitignored).

---

## Appendix A — Inbound fast path (shared bidirectional cache)

Everything in §3–§8 measures the **send** side. Receiving has the same
per-message shape in reverse — Askar session, key lookup, unpack FFI, then a
wallet round-trip in the dispatcher to re-find the `ConnRecord`. The plugin
extends the *same* cached connection object to cover it: the X25519 keypair
cached for outbound authcrypt is the private key that decrypts inbound messages
addressed to that verkey, so inbound and outbound share one
`CachedConnectionCrypto` (one set of native key handles, one TTL/LRU/invalidation
lifecycle), with a secondary index by `(wallet_id, local recipient verkey)`.

### A.1 Results

Wire-valid authcrypt BasicMessage replayed straight at the DIDComm inbound
endpoint; 10 000 messages, 20 concurrent posters, one connection. Completion is
counted at the stock BasicMessage handler's `received` event, **not** HTTP
acceptance.

| Inbound path | Handled msg/s | Failures | Cache hits / misses | Hot unpack mean | Cold unpack mean |
|---|---:|---:|---|---:|---:|
| Stock (`FASTPATH_INBOUND=0`) | 174.7 | 0 | — | — | — |
| Fast path (`FASTPATH_INBOUND=1`) | **210.5** | 0 | 9 901 / 99 | **0.80 ms** | 53.2 ms |

- **~1.2× on the full inbound pipeline.** Hot cached unpack is ~66× faster than
  the cold/stock unpack (0.80 ms vs 53.2 ms); the remaining ~4 ms/msg is the
  stock dispatcher, handler, and event plumbing the fast path leaves untouched.
- The 99 misses are the initial warm plus one TTL-expiry batch mid-run (the 30 s
  `FASTPATH_CACHE_TTL` elapsed inside the 47 s run); each fell back cleanly to
  stock unpack and re-warmed the shared object.
- Issuer CPU ~108% mean / ~122% peak — the same single-core process bound as the
  send side.

### A.2 Design & security

- **Two integration points, both installed by the plugin at load** (nothing in
  the ACA-Py package is edited on disk):
  1. a `BaseWireFormat` injector binding (supported extension point) that tries a
     cached inline decrypt first and falls back to the fully stock unpack on any
     miss, which then warms the shared object;
  2. a runtime override of `BaseConnectionManager.find_inbound_connection` so the
     dispatcher reuses the already-authenticated cached `ConnRecord` instead of a
     per-message wallet round-trip. This is a **monkeypatch** — version-sensitive;
     it delegates to the stock method whenever no cached record is attached, and
     `FASTPATH_INBOUND=0` disables all of it.
- **Security posture unchanged:** hits require the exact `(wallet_id, recipient
  verkey)` index entry; authcrypt sender verification still runs (decrypted
  sender verkey must match the cached connection's); the `ConnRecord` shortcut is
  only taken for authcrypt (anoncrypt has no authenticated sender); the same
  TTL/LRU/invalidation applies since it is literally the same cache entry.

### A.3 Run it

From the repo root, with the benchmark stack built (`bash
scripts/run-basicmsg-benchmark.sh up`):

```bash
# 1. Fast-path inbound (default FASTPATH_INBOUND=1). Recreate the issuer to be sure:
FASTPATH_INBOUND=1 docker compose -p owl-benchmark \
  -f docker-compose.demo.yml -f docker-compose.benchmark.yml \
  --env-file sample.benchmark.env up -d --force-recreate issuer

# wait for live
until curl -sf http://localhost:8150/status/live >/dev/null; do sleep 2; done

# replay 10k authcrypt BasicMessages from a helper container on the bench network
docker run --rm --entrypoint python --network owl-benchmark_app-network \
  -v "$PWD/scripts:/bench:ro" acapy-cache-redis \
  /bench/run-inbound-benchmark.py --messages 10000 --concurrency 20

# 2. Stock baseline — same replay against a stock inbound issuer:
FASTPATH_INBOUND=0 docker compose -p owl-benchmark \
  -f docker-compose.demo.yml -f docker-compose.benchmark.yml \
  --env-file sample.benchmark.env up -d --force-recreate issuer
until curl -sf http://localhost:8150/status/live >/dev/null; do sleep 2; done
docker run --rm --entrypoint python --network owl-benchmark_app-network \
  -v "$PWD/scripts:/bench:ro" acapy-cache-redis \
  /bench/run-inbound-benchmark.py --messages 10000 --concurrency 20
```

The script creates one static connection, warms the shared object with a single
outbound send, then replays a wire-valid authcrypt envelope while polling
`GET /didcomm-fastpath/stats`. It prints JSON with `handled_rps`,
`cache_hits`/`cache_misses`, and hot vs cold unpack timings. Flags:
`--messages`, `--concurrency`, `--admin-url`, `--inbound-url`, `--timeout`.

Raw artifacts for the runs above: `results/basicmsg/inbound-fastpath/` and
`results/basicmsg/inbound-stock/` (summarized in
`results/basicmsg/INBOUND.md`).
