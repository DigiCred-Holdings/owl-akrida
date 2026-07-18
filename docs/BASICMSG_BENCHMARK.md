# Local ACA-Py Basic Message Benchmark

Harness for reproducing ACA-Py DIDComm basic-message throughput (stock, isolation,
`didcomm_fastpath`, and mock-holder sink).

**Start here for replication:** [`REPLICATE_THROUGHPUT.md`](./REPLICATE_THROUGHPUT.md)  
**Full investigation report:** [`ACAPY_THROUGHPUT_REPORT.md`](./ACAPY_THROUGHPUT_REPORT.md)  
**Phase notes:** [`throughput/`](./throughput/)

## Layout

| Path | Role |
|---|---|
| `load-agent/locust-files/locustBasicMsgBenchmark.py` | Exact-count Locust scenario |
| `load-agent/basicmsg_counter.py` | Steady-state RPS counter |
| `docker-compose.benchmark.yml` | Overlay: env, live mounts, mock-holder, py-spy caps |
| `instance-configs/acapy-agent/configs/issuer-*.yml` | Issuer arg-files (benchmark / isolated / fastpath) |
| `instance-configs/acapy-agent/plugins/didcomm_fastpath/` | Fast-path send plugin |
| `mock-holder/` | HTTP sink (200, no DIDComm unpack) |
| `sample.benchmark.env` | Default env for Compose |
| `scripts/run-basicmsg-benchmark.sh` | Orchestrator (`up` / `run` / `isolate` / `matrix`) |

## Quick start

From the **repository root**:

```bash
bash scripts/run-basicmsg-benchmark.sh up
bash scripts/run-basicmsg-benchmark.sh run direct-20x1

# Isolation matrix (stock ceiling, ~2k msgs)
TARGET_MESSAGE_COUNT_OVERRIDE=2000 bash scripts/run-basicmsg-benchmark.sh isolate

# Fast-path + mock sink ceiling
TARGET_MESSAGE_COUNT_OVERRIDE=2000 LOCUST_USERS_OVERRIDE=60 \
  bash scripts/run-basicmsg-benchmark.sh run fastpath-pg-admin-sink-60
```

Artifacts: `results/basicmsg/<profile>/` (Locust CSV/HTML, docker stats, logs,
`fastpath-stats.json`, `mock-holder-stats.json`). That directory is gitignored;
summary markdown is under `docs/throughput/`.

## Profiles

| Profile | Shape | Notes |
|---|---|---|
| `direct-1x20` | 1×20 | Serial harness path |
| `direct-20x1` | 20×1 | Concurrent requests into ACA-Py |
| `mediated-*` | same via mediator | Mediation/pickup cost |
| `direct-20x1-pool50` | concurrent + pool 50 | Pool-saturation check |
| `direct-20x1-debug` | concurrent + debug logs | Logging overhead |
| `isolate-pg-*` / `isolate-sqlite-*` | 20×1, no mediator/Redis | Storage & measure-mode isolation |
| `fastpath-pg-admin` / `fastpath-pg-e2e` | 20×1 | Fast-path → Credo |
| `fastpath-pg-admin-sink[-N]` | N×1 | Fast-path → mock sink |

```bash
bash scripts/run-basicmsg-benchmark.sh help
```
