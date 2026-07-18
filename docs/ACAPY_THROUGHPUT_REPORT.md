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

## 7. What we know vs. don't

**Know (measured on 1.6.0):** the ~58–70 stock ceiling and its cause (per-send Askar/FFI + event
loop, not storage/mediator/crypto); the fast path's ~1.35–1.5× lift at lower CPU/msg with an
identical wire format; a single-process ceiling of ~242 msg/s with a cheap recipient.

**Don't (open):**
- **Absolute ceiling on dedicated hardware** — even sink runs co-locate the load generator; a
  remote generator or CPU-pinned issuer would give a cleaner number.
- **py-spy split is directional** — ~40% of samples dropped to native unwinding; treat the
  percentages as shares, not exact.
- **Horizontal scaling linearity** — expected ~N×~240 behind shared Postgres, not yet measured.
- **Real mediated wallets** differ from local Credo holders; our numbers bound the *issuer*.
- **Production hardening of the plugin** — cache invalidation is done (event-bus eviction on
  `connections` events + `FASTPATH_CACHE_TTL`); send-side BasicMessage persistence/webhooks and
  mediator forward-wrapping remain untested.

---

## 8. Recommendations

- **Ceiling:** treat **~200–240 msg/s per fast-path process** as the working per-process budget;
  **scale horizontally** behind shared Postgres and validate linearity with 2–4 replicas.
- **Storage tuning is a dead end** past an adequate Askar pool; keep Redis only for setup-time
  pool pressure.
- **For a clean absolute number:** rerun §6 with the load generator on a separate host (or
  `cpuset`-pin the issuer).
- **Deeper single-process gains (only if needed):** ECDH shared-secret cache inside pack; tune
  `FASTPATH_PACK_WORKERS`.

---

## 9. Reproduce

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
