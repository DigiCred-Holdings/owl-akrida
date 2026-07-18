# kanon_storage

Postgres-backed ACA-Py wallet storage backend. Single shared Postgres DB instead of Askar's per-wallet sqlite files.

## What it does

Implements ACA-Py's `BaseStorage` + `BaseStorageSearch` over SQLAlchemy/asyncpg. Every record (DIDs, keys, generic records, outbox, AnonCreds objects) lives in Postgres with a `profile_id` column scoping it to one tenant. Lets operators run a single Postgres for the whole multitenant fleet with normal SQL tooling for debugging and ops.

## How it works

- **Profile managers** — `KanonStorageProfileManager` (single-tenant) and `KanonStorageMultitenantManager` register against `wallet.type = "kanon-storage-anoncreds"` / `"kanon-storage-multi"`.
- **Adapters** — `GenericRecordAdapter` handles untyped records; typed adapters exist for DIDs, keys, outbox, storage-version metadata. Each adapter takes a `profile_id` and a SQLAlchemy session, and emits queries scoped by `(profile_id, record_type, id)`.
- **Engine cache** — module-level `_ENGINE_CACHE` keyed by database URL plus an `asyncio.Lock` for double-checked init. `ProfileManager.open()` and `.provision()` are invoked on every profile-open path; without caching, each call would leak a fresh 30+30 connection pool.
- **Outbox** — write-ahead log table for crash recovery. Replayed on every profile open; counts always logged so a zero-row replay is distinguishable from a skipped one.
- **`_profile_id` resolution** — raises `ConfigError` if no `wallet.id` / `wallet.name` is resolvable. No silent `"default"` fallback (which would merge every misconfigured tenant's data into one profile).

## Wire-level shape

Every record table follows the pattern:

```sql
kanon_<record>(profile_id, record_type, id, value, tags, ...)
```

with `(profile_id, record_type, id)` as the composite primary key. Tags live in a `JSONB` column with GIN indexes for the common `WHERE tag @> '{...}'` queries.

## Activation

In `acapy-args.yml`:

```yaml
wallet.type: kanon-storage-multi    # or kanon-storage-anoncreds for single-tenant
multitenant.enabled: true            # required for kanon-storage-multi
```

In `plugin-config.yml`:

```yaml
kanon_storage:
  database_url: postgresql+asyncpg://user:pass@host:5432/db
  pool_size: 30
  pool_overflow: 30
  auto_migrate: false                # default; opt in on first-time deploys (see below)
  master_key: <32-byte hex>           # AES-256 key for at-rest record encryption
```

`KANON_STORAGE_DATABASE_URL` and `KANON_STORAGE_MASTER_KEY` env vars override the plugin-config values.

## Migrations

Alembic migrations live under `kanon_storage/v1_0/db/migrations/`.

`auto_migrate` defaults to **false** so an upgrade against an existing deployment never silently no-ops — populated schemas must always be advanced via Alembic, and leaving the flag on would mask a missed migration as "startup succeeded" until a feature requiring the new column fails with a cryptic `column not found` later.

**First-time deploy (empty DB):** set `auto_migrate: true` in `plugin-config.yml` for the first boot. The plugin runs `metadata.create_all` only when the public schema is empty, then no-ops; flip it back to `false` (or leave it on — subsequent boots are idempotent). For any deploy where the schema is already populated, leave `auto_migrate: false` and run Alembic explicitly.

## Dependencies

None at the plugin layer. At runtime: a Postgres ≥ 14 instance and `asyncpg` (declared in `pyproject.toml`).

Foundation for `did_kanon`.
