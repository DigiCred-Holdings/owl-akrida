# didcomm_fastpath (benchmark copy)

Experimental fast-path DIDComm v1 basic-message send pipeline for ACA-Py.

Installed into the issuer image by `instance-configs/acapy-agent/docker/Dockerfile`
and live-mounted by `docker-compose.benchmark.yml`.

Enable via issuer arg-file `issuer-isolated-pg-fastpath.yml` (`plugin: didcomm_fastpath`).

| Method | Path |
|---|---|
| POST | `/didcomm-fastpath/connections/{conn_id}/send-message` |
| GET / DELETE | `/didcomm-fastpath/stats` |
| DELETE | `/didcomm-fastpath/cache` |

Env:

- `FASTPATH_DELIVER_OVERRIDE` — redirect HTTP deliver to a sink (e.g. `http://mock-holder:8090/`)
- `FASTPATH_PACK_WORKERS` — pack thread pool size (default 32)
- `FASTPATH_CACHE_TTL` — absolute entry age in seconds (default **30**; `0` disables); lazy + ~1s active sweep
- `FASTPATH_CACHE_MAX` — max cached targets, LRU (default 8192; `0` unbounded); size by peak concurrent active connections, not total

Cache key = `(local_tenant_wallet_id, connection_id)` — `wallet_id` is *our* ACA-Py
tenant subwallet, not the remote peer. Hardening: ConnRecord eviction, active TTL,
LRU, `invalidate_wallet` / `acapy::didcomm_fastpath::wallet_removed`, disposal metrics.

See [`docs/REPLICATE_THROUGHPUT.md`](../../../../docs/REPLICATE_THROUGHPUT.md) and
[`docs/ACAPY_THROUGHPUT_REPORT.md`](../../../../docs/ACAPY_THROUGHPUT_REPORT.md).

Production-shaped copy (CRMS): `digicred-crms` plugin `didcomm_fastpath`.
