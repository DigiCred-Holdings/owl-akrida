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
- `FASTPATH_CACHE_TTL` — cached-target expiry in seconds (default 300; `0` disables)

Cache invalidation: any `connections` record event (update, DID rotation, deletion) evicts
that connection's cached target via an event-bus subscription; TTL is the fallback.

See [`docs/REPLICATE_THROUGHPUT.md`](../../../../docs/REPLICATE_THROUGHPUT.md) and
[`docs/ACAPY_THROUGHPUT_REPORT.md`](../../../../docs/ACAPY_THROUGHPUT_REPORT.md).

Production-shaped copy (CRMS): `digicred-crms` branch `feat/didcomm-fastpath-plugin`.
