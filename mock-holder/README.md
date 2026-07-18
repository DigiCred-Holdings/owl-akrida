# mock-holder

Lightweight DIDComm message sink for issuer throughput isolation.

Accepts any HTTP POST and returns `200` without unpacking. Used with
`FASTPATH_DELIVER_OVERRIDE=http://mock-holder:8090/` so packing still uses real
connection keys while delivery skips Credo holder CPU.

```bash
# Started automatically by sink profiles in scripts/run-basicmsg-benchmark.sh
curl -s localhost:8090/health
curl -s localhost:8090/stats
```

See [`docs/REPLICATE_THROUGHPUT.md`](../docs/REPLICATE_THROUGHPUT.md).
