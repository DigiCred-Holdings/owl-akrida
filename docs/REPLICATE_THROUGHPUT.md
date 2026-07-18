# ACA-Py Basic-Message Throughput — Replication Guide

Reproduce the DigiCred investigation of ACA-Py DIDComm basic-message throughput on
**ACA-Py 1.6.0** (stock ~58–70 msg/s → fast-path ~94–105 against Credo → ~220–242 against a
mock sink).

**Full report (problem, results, known vs unknown):**
[`ACAPY_THROUGHPUT_REPORT.md`](./ACAPY_THROUGHPUT_REPORT.md)

**Phase write-ups:** [`throughput/`](./throughput/)

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Compose v2 | Tested on Linux / WSL2 |
| ~8 GB free RAM | Comfortable for 20 holders; 40–60 need more |
| Ports free | `8150`, `8151`, `8090` (mock sink), `5432`, `6379`, `8089` |
| Git | Clone this repo / checkout the branch below |

**Recommended host for the sink ceiling:** ≥8 vCPU. The load generator (Credo holders)
shares the host with the issuer; on a small box holders can starve the issuer and
understate the ceiling (see the report §6.5).

---

## Checkout

```bash
git clone git@github.com:DigiCred-Holdings/owl-akrida.git
cd owl-akrida
git checkout benchmark/basic-msg-10k
```

---

## One-command quick paths

All commands below are from the **repo root**.

### A. Stock ACA-Py ceiling (~58–70 msg/s) — isolation matrix

```bash
bash scripts/run-basicmsg-benchmark.sh up          # build issuer + DBs (+ optional mediator)
TARGET_MESSAGE_COUNT_OVERRIDE=2000 \
  bash scripts/run-basicmsg-benchmark.sh isolate
```

Artifacts: `results/basicmsg/isolate-*/` and `results/basicmsg/ISOLATION.md`.

### B. Fast-path against real Credo holders (~94 admin / ~105 e2e)

```bash
TARGET_MESSAGE_COUNT_OVERRIDE=2000 \
  bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin

TARGET_MESSAGE_COUNT_OVERRIDE=2000 \
  bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-e2e
```

Live timings:

```bash
curl -s localhost:8150/didcomm-fastpath/stats | python3 -m json.tool
```

### C. True issuer ceiling with mock holder (~220–242 msg/s)

Credo still establishes real connections (keys). Delivery is redirected to a
lightweight HTTP sink that returns `200` without unpacking.

```bash
for n in 20 40 60; do
  TARGET_MESSAGE_COUNT_OVERRIDE=2000 LOCUST_USERS_OVERRIDE=$n \
    bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin-sink-$n
done
```

Sink counters:

```bash
curl -s localhost:8090/stats | python3 -m json.tool
```

### Tear down

```bash
bash scripts/run-basicmsg-benchmark.sh down    # keep volumes
bash scripts/run-basicmsg-benchmark.sh reset   # remove volumes
```

---

## What gets built

`scripts/run-basicmsg-benchmark.sh` uses:

| Piece | Path |
|---|---|
| Compose base | `docker-compose.demo.yml` |
| Benchmark overlay | `docker-compose.benchmark.yml` |
| Env defaults | `sample.benchmark.env` |
| Issuer image | `instance-configs/acapy-agent/docker/Dockerfile` (ACA-Py 1.6.0 on py3.13 + redis cache + `py-spy` + `didcomm_fastpath`) |
| Fast-path plugin | `instance-configs/acapy-agent/plugins/didcomm_fastpath/` |
| Mock holder sink | `mock-holder/` |
| Locust scenario | `load-agent/locust-files/locustBasicMsgBenchmark.py` |

First run builds images (`--build`). Later runs recreate the issuer with the selected
arg-file. The plugin source is **live-mounted** into the issuer container so edits
apply without rebuilding (restart issuer still required).

---

## Profiles at a glance

| Profile | What it measures |
|---|---|
| `direct-20x1` / `direct-1x20` / `mediated-*` | Full-stack reproduction (10k default) |
| `isolate-pg-e2e` / `isolate-sqlite-*` / `isolate-*-admin` | Mediator/Redis off; storage & measure-mode isolation |
| `fastpath-pg-admin` / `fastpath-pg-e2e` | Fast-path plugin → real Credo holders |
| `fastpath-pg-admin-sink[-N]` | Fast-path pack with real keys → mock sink |

Override concurrency / message count without editing files:

```bash
TARGET_MESSAGE_COUNT_OVERRIDE=2000 LOCUST_USERS_OVERRIDE=40 \
  bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin-sink-40
```

List commands / profiles:

```bash
bash scripts/run-basicmsg-benchmark.sh help
```

---

## Expected ballpark numbers (ACA-Py 1.6.0, this harness, 12 vCPU WSL2)

| Scenario | Steady RPS (order of magnitude) |
|---|---:|
| Stock concurrent (`isolate-pg-admin`) | ~58–70 |
| Fast-path → Credo (`fastpath-pg-admin` / `-e2e`) | ~94 / ~105 |
| Fast-path → mock sink @ 20 holders | ~220 |
| Fast-path → mock sink @ 60 holders | ~240 |

Exact numbers vary by host. Compare **ratios and relative gains**, and check
`fastpath-stats.json` / `mock-holder-stats.json` under each run directory.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Issuer never goes live | Image build / wallet DB | `docker compose -p owl-benchmark logs issuer` |
| `Cannot connect to host mock-holder` | Sink not on `app-network` | Recreate: `… up -d --force-recreate mock-holder` (overlay attaches `app-network`) |
| Throughput stuck ~90 with many holders | Credo holders saturating host CPU | Use `*-sink` profiles, or move load-gen to another host |
| `pool timed out` on connection setup | Askar pool too small, Redis off | Isolation profiles already set `max_connections: 30` |
| Port range exhausted | Too many holders | Raise `END_PORT` in `sample.benchmark.env` |

---

## Related plugin (CRMS)

Production-shaped copy of the plugin (Poetry, tenant auth, `v1_0/` layout):

- Repo: [DigiCred-Holdings/digicred-crms](https://github.com/DigiCred-Holdings/digicred-crms)
- Branch: `feat/didcomm-fastpath-plugin`
- Path: `plugins/didcomm_fastpath/`

Enable with:

```yaml
plugin:
  - didcomm_fastpath
```
