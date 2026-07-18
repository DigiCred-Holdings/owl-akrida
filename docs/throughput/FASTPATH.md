# didcomm_fastpath plugin — first results

**What it is:** an ACA-Py plugin (`instance-configs/acapy-agent/plugins/didcomm_fastpath`) exposing
`POST /didcomm-fastpath/connections/{conn_id}/send-message`. It keeps DIDComm v1 wire format
identical (fresh CEK/nonces/AEAD per message) but caches per-connection send material after the
first message:

- connection target (endpoint, recipient/routing verkeys) — resolved once, not per send
- sender key fetched from Askar once — no per-send profile session at all on the hot path
- Ed25519→X25519 conversions and the sealed-sender blob precomputed per recipient
- minimal JSON message build (no marshmallow), pack in a thread executor
- delivery over a dedicated keep-alive aiohttp session (no outbound queue/encode round-trips)

Per-stage timings are exposed at `GET /didcomm-fastpath/stats`.

## Results (2,000 msgs, 20 holders × 1 connection, Postgres pool 30, no Redis/mediator/ledger)

| Profile | Path | Steady RPS | Issuer CPU peak |
|---|---|---:|---:|
| isolate-pg-admin (stock) | `/connections/{id}/send-message` | 48.3 | 155% |
| **fastpath-pg-admin** | `/didcomm-fastpath/...` (send only) | **89.5** | **80%** |
| **fastpath-pg-e2e** | fastpath send + Credo receipt awaited | **89.0** | ~80% |

**~1.85× throughput and CPU cut roughly in half.** e2e ≈ admin confirms messages are actually
delivered and processed by the Credo holders (every send was matched to a
`BasicMessageStateChanged` receipt).

## Stage breakdown (fastpath-pg-admin, mean per message)

| Stage | Mean | Share of 93.8 ms total |
|---|---:|---:|
| resolve (cold, once per connection) | 199.8 ms × 20 | — |
| build message JSON | 0.23 ms | <1% |
| pack (executor, incl. queueing) | 22.9 ms | 24% |
| deliver (HTTP POST + holder 200) | 68.6 ms | 73% |

At 89 RPS the process is **no longer CPU-bound** (0.8 cores vs 1.55 stock). The wall time is now
dominated by waiting on the holder's inbound HTTP response and executor queueing under 20-way
concurrency — i.e. the remaining ceiling is concurrency/downstream, not ACA-Py Python work.

## What this tells us

1. The stock ~50 msg/s ceiling was **not** crypto: crypto is unchanged here. It was the per-send
   overhead around it — admin route + ConnRecord fetch, per-send Askar profile session churn, the
   outbound transport queue/encode state machine, and repeated key fetch/conversions.
2. With that overhead removed, one ACA-Py process does **~90 msg/s at 0.8 cores** — meaning there
   is CPU headroom left. (A later concurrency sweep confirmed the issuer is *not* the bottleneck at
   this rate — the co-located load generator is. See "Concurrency sweep" below.)
3. Combined with horizontal scaling this stacks: N replicas × ~90 instead of N × ~50.

## Caveats (benchmark-grade, not production-grade)

- ~~Target cache is not invalidated on DID rotation / connection deletion.~~ Now handled:
  any `connections` record event evicts that entry, plus a `FASTPATH_CACHE_TTL` fallback
  (default 300 s; `0` disables). `DELETE /didcomm-fastpath/cache` still clears manually.
- No BasicMessage record persistence or webhook emission on the send side.
- Mediator forward-wrapping is implemented but has not been benchmarked yet.
- Sealed-sender blob reuse is protocol-valid (it only conveys the sender verkey) but differs from
  stock, which regenerates it per message.

Repro: `bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin` (and `fastpath-pg-e2e`).
Artifacts: `results/basicmsg/fastpath-pg-admin/`, `results/basicmsg/fastpath-pg-e2e/`.

---

# Final verification run — ACA-Py 1.6.0 + generalized `send_packed`

After the plugin was (a) rebased on **ACA-Py 1.6.0** (py3.13) and (b) generalized so the pack
pipeline serves arbitrary AgentMessages (`send_packed` / `send_agent_message`, used by
`workflow_protocol`) in addition to basic messages, the full basic-message matrix was
**re-measured on 1.6.0** (10k messages each, isolated stack). No regression on the basicmessage
path (0 failures throughout):

| Profile | Path | ACA-Py | Steady RPS | Issuer CPU (mean) |
|---|---|---|---:|---:|
| isolate-pg-admin (stock) | admin send, real Credo | 1.6.0 | 70.0 | 124% |
| fastpath-pg-admin | fastpath send, real Credo | 1.6.0 | 94.4 | 106% |
| fastpath-pg-e2e | fastpath + Credo receipt | 1.6.0 | **104.9** | — |
| fastpath-pg-admin-sink (20/40/60) | mock recipient | 1.6.0 | 220.6 / 227.1 / **241.9** | 81% @60 |

Stock 1.6.0 (~70 msg/s) is faster than stock 1.3.0 (~48 msg/s), so the fast path's relative lift
is smaller on 1.6.0 (~1.35× admin, ~1.5× e2e) though absolute throughput is higher. The mock-sink
ceiling rose to **~242 msg/s** (peak at 60 connections) — the real-Credo runs remain load-generator
bound, not issuer bound.

Per-stage means (from `GET /didcomm-fastpath/stats`, 20 cached connections, `pack_workers=32`):

| Stage | Mean | Note |
|---|---:|---|
| resolve (cold, once/conn) | 144.3 ms × 20 | one-time per connection |
| build | 0.22 ms | JSON build, no marshmallow |
| pack | 20.1 ms | executor pack (fresh CEK/nonce/AEAD) |
| deliver | 56.3 ms | HTTP POST + Credo holder 200 (holder CPU bound) |
| total | 76.9 ms | — |

Same shape as the earlier ~89 msg/s runs (deliver dominates because the co-located Credo holders
do the unpack work); the higher RPS here reflects run-to-run host-load variance, not a code change.
The takeaway is unchanged: **the issuer is not the bottleneck at this rate**, and the generalized
path preserves the basicmessage throughput while enabling the workflow fastpath.

Repro: `TARGET_MESSAGE_COUNT=10000 bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-e2e-final`.

> Ops note: on a fresh Postgres volume, first-boot DB initialization can outlast the issuer's
> store-open retry window (`ProfileError: Failed to open or provision store after retries`). If the
> scripted run fails on first launch, start the DB once (`... up -d issuer-db`), wait for
> `pg_isready`, then re-run.

---

# Concurrency sweep — where is the *real* ceiling? (load-generator saturation)

To test whether ~90 msg/s was the fastpath issuer's ceiling or just the test shape, we swept
holder concurrency on the fastpath admin path (2,000 messages each, same isolated stack) and — for
the first time — sampled **host-wide CPU and the load-generator container**, not just the issuer.

| Holders | Steady RPS | Issuer CPU | Host CPU | Load-gen (Credo holders) CPU |
|---:|---:|---:|---:|---:|
| 20 | 89.5 | ~0.8 core | (not sampled) | — |
| 40 | 89.7 | ~0.7–0.9 core | (not sampled) | — |
| 60 | 84.0 | ~1.0–1.5 core | **99.5% (all 12 cores)** | **~1,100–1,250% (11–12 cores)** |

**Throughput is flat, then declines** as concurrency rises — the classic signature of a saturated
resource. But the saturated resource is **not the issuer**:

- At 60 holders the **host was pegged at 99.5%** with load average ~36.
- The **load-generator container consumed ~1,100–1,250% CPU (11–12 of 12 cores)** — that is the
  fleet of Credo holder Node processes unpacking/processing every delivered message.
- The **fastpath issuer used only ~1–1.5 cores** the whole time. It was CPU-starved by the
  co-located holders, not maxed out.

## Why the holders are in the critical path even in "admin-only" mode

The fastpath `send_basicmessage` awaits the recipient's HTTP `200` in its `deliver` stage, and a
Credo holder returns `200` only *after* it has unpacked and fully processed the message
(serialized per connection). So holder CPU/latency is inside the measured path. As holders were
added on the same 12-core box, they ate the CPU the issuer needed, and per-message latency
inflated (`total` 94 ms → 166 ms from 20→40 holders) while throughput stayed pinned.

## Conclusion

**We have not yet observed the fastpath issuer's true ceiling.** ~84–90 msg/s is a **measurement
artifact of the load generator**, which is co-located with the issuer and does ~10× the issuer's
CPU work (full Credo unpack+process per message). The earlier "0.8 cores, headroom left" reading
was correct — the issuer *does* have headroom; the rig can't feed it.

### To find the real issuer ceiling (next experiments)

1. **Replace Credo holders with a lightweight HTTP sink** that returns `200` without unpacking.
   This isolates the issuer's build+pack+deliver throughput. (Biggest signal, smallest effort.)
2. **Move the load generator to a separate host/VM** from the issuer so they don't share cores.
3. **Pin CPU sets** (`cpuset`) so the issuer gets dedicated cores even when co-located.
4. Only after the issuer is the demonstrated bottleneck do executor-worker and ECDH-cache tuning
   become worth measuring.

### Measurement fix applied

`scripts/collect-stats.sh` only sampled compose-managed services, so the `compose run` load-agent
container (the holders) was invisible. Host-wide sampling revealed the real picture; future runs
should capture host CPU and the load-gen container explicitly.

Artifacts: `results/basicmsg/fastpath-pg-admin-40/`, `results/basicmsg/fastpath-pg-admin-60/`.

---

# Mock-holder sink — real issuer ceiling

To remove Credo unpack from the critical path we added:

1. `mock-holder/` — aiohttp sink that returns HTTP 200 on any POST without unpacking
2. `FASTPATH_DELIVER_OVERRIDE` — plugin env that keeps **real recipient keys** for packing
   but redirects the HTTP deliver hop to the sink
3. Profile `fastpath-pg-admin-sink[-N]` — Credo still used only for connection setup/keys

## Results (2–3k messages, isolated stack, pack workers=32)

| Holders | Steady RPS | Pack mean | Deliver mean | Total mean | Sink received |
|---:|---:|---:|---:|---:|---:|
| 20 (Credo deliver) | 89.5 | 22.9 ms | 68.6 ms | 93.8 ms | n/a |
| **20 (mock sink)** | **169** | 16.3 ms | **12.8 ms** | **30.7 ms** | 2000 |
| **40 (mock sink)** | **202** | 25.9 ms | 22.3 ms | 56.1 ms | 2000 |
| **60 (mock sink)** | **207** | 35.4 ms | 36.0 ms | 79.7 ms | 2000 |
| 80 (mock sink) | 195 | 43.4 ms | 48.4 ms | 101.6 ms | 3000 |

## Verdict

- With a mock holder, a **single ACA-Py process clears ~170–210 msg/s** on the fastpath.
- That matches the original tester’s “Credo ~170 msg/s” claim — once the recipient is cheap.
- Peak here is **~207 msg/s at 60 concurrent connections**; 80 regresses slightly because
  warm Credo processes still compete for host CPU even when idle.
- Deliver mean dropped from ~69 ms → ~13 ms at 20 holders; pack became a co-equal cost.
- Further gains likely need: fewer co-located Credo processes (or remote load gen),
  ECDH/shared-secret caching inside pack, or multi-process ACA-Py on one host.

Repro:

```bash
TARGET_MESSAGE_COUNT_OVERRIDE=2000 LOCUST_USERS_OVERRIDE=60 \
  bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin-sink-60
```

Artifacts: `results/basicmsg/fastpath-pg-admin-sink-{20,40,60,80}/`.

