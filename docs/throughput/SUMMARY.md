# ACA-Py Basic Message Benchmark Summary

Results root: `/home/development/digicred/owl-akrida-benchmark/results/basicmsg`

| Run | Users | Conns/user | Mediation | msgs | fail | steady RPS | p50 ms | p95 ms | p99 ms | issuer CPU% peak | notes |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| direct-1x20 | 1 | 20 | false | 10000 | 0 | 25.02 | 35 | 64 | 93 | 54.8 | t=399.7s |
| direct-1x20-r2 | 1 | 20 | false | 10000 | 0 | 23.79 | 36 | 70 | 100 | 58.3 | t=420.3s |
| direct-20x1 | 20 | 1 | false | 10000 | 0 | 46.87 | 410 | 550 | 640 | 149.3 | t=213.4s |
| direct-20x1-debug | 20 | 1 | false | 10000 | 0 | 45.32 | 430 | 550 | 680 | 145.8 | debug logs;t=220.6s |
| direct-20x1-pool50 | 20 | 1 | false | 10000 | 0 | 51.25 | 380 | 500 | 580 | 149.9 | pool=50;t=195.1s |
| direct-20x1-r2 | 20 | 1 | false | 10000 | 0 | 47.43 | 410 | 540 | 610 | 152.1 | t=210.8s |
| isolate-pg-admin | 20 | 1 | false | 2000 | 0 | 48.30 | 390 | 560 | 640 | 155.0 | t=41.4s |
| isolate-pg-e2e | 20 | 1 | false | 2000 | 0 | 50.29 | 370 | 640 | 720 | 162.1 | t=39.8s |
| isolate-sqlite-admin | 20 | 1 | false | 2000 | 0 | 47.69 | 400 | 610 | 710 | 163.1 | t=41.9s |
| isolate-sqlite-e2e | 20 | 1 | false | 2000 | 0 | 55.15 | 340 | 500 | 690 | 154.8 | t=36.3s |
| mediated-1x20 | 1 | 20 | True | 10000 | 0 | 17.13 | 52 | 90 | 120 | 47.5 | t=583.8s |
| mediated-20x1 | 20 | 1 | True | 10000 | 0 | 31.08 | 650 | 860 | 1000 | 112.0 | exit=1;t=321.8s |

## Interpretation guide

- Compare `direct-1x20` vs `direct-20x1`: if serial stays near concurrent throughput, the harness or a single-threaded server path is likely limiting.
- Compare direct vs mediated: mediation/pickup overhead is the delta.
- Compare `direct-20x1` vs `direct-20x1-pool50`: pool saturation vs application-level serialization.
- Compare `direct-20x1` vs `direct-20x1-debug`: logging overhead.
- Only conclude ACA-Py event-loop serialization if concurrent load fails to scale, issuer CPU is ~1 core saturated, and Postgres/mediator/load-gen are not the limiters.
