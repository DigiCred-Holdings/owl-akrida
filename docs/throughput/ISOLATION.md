# ACA-Py Isolation Results

| Profile | Steady RPS | Measure | Storage | Redis |
|---|---:|---|---|---|
| isolate-pg-e2e | 50.29 | e2e | postgres | 0 |
| isolate-sqlite-e2e | 55.15 | e2e | sqlite | 0 |
| isolate-pg-admin | 48.30 | admin_only | postgres | 0 |
| isolate-sqlite-admin | 47.69 | admin_only | sqlite | 0 |
| direct-20x1 | 46.87 | e2e | postgres | n/a |

Interpretation: if SQLite ≈ Postgres, storage is not the ceiling. If admin_only ≫ e2e, holder/receipt path dominates the measured latency. If both stay near ~50 RPS with issuer CPU ~1.5 cores, ACA-Py processing itself is the limiter.
