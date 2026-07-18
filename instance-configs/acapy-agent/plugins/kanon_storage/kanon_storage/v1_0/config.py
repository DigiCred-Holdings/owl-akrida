"""Kanon Storage configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from acapy_agent.config.settings import BaseSettings

KS_PREFIX = "kanon_storage."

SETTING_DATABASE_URL = KS_PREFIX + "database_url"
SETTING_MASTER_KEY = KS_PREFIX + "master_key"
SETTING_POOL_SIZE = KS_PREFIX + "pool_size"
SETTING_POOL_OVERFLOW = KS_PREFIX + "pool_overflow"
SETTING_POOL_TIMEOUT = KS_PREFIX + "pool_timeout_s"
SETTING_POOL_RECYCLE = KS_PREFIX + "pool_recycle_s"
SETTING_ECHO_SQL = KS_PREFIX + "echo_sql"
SETTING_AUTO_MIGRATE = KS_PREFIX + "auto_migrate"

DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///:memory:"
# Pool sized for ACA-Py's typical concurrent-session profile under load:
# auto-advance fans out one session per active workflow instance per
# event, plus admin polling, plus DIDComm handlers. 10/5 deadlocked
# under workflow_protocol e2e where 15+ concurrent sessions are normal.
DEFAULT_POOL_SIZE = 30
DEFAULT_POOL_OVERFLOW = 30
DEFAULT_POOL_TIMEOUT_S = 30
# Recycle pooled connections after this many seconds. asyncpg/Postgres
# connections can be killed by the server (idle timeout, pg_terminate_backend,
# NAT) without a TCP RST; `pool_pre_ping` catches dead conns at checkout but
# is a band-aid. Recycling before the server's own idle timeout (cloud
# Postgres default ~9h, PgBouncer often shorter) prevents holding stale
# conns indefinitely.
DEFAULT_POOL_RECYCLE_S = 1800
# Off by default: auto-bootstrap (metadata.create_all) is only safe on an
# empty schema, and a populated DB must be advanced via Alembic. Defaulting
# to True silently no-ops on existing deployments — the operator's first
# clue something is wrong is a cryptic "column not found" much later.
# Operators who want first-deploy bootstrap must opt in explicitly in
# plugin-config.yml (auto_migrate: true).
DEFAULT_AUTO_MIGRATE = False


def _redact_url_for_repr(url: str) -> str:
    """Strip password from a SQLAlchemy URL for safe logging/repr."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, _, host = rest.partition("@")
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return url


@dataclass(frozen=True)
class KanonStorageConfig:
    """Resolved configuration for a single Kanon Storage profile.

    Construct via :meth:`from_settings` from the live ACA-Py settings.
    """

    database_url: str
    master_key: bytes
    pool_size: int = DEFAULT_POOL_SIZE
    pool_overflow: int = DEFAULT_POOL_OVERFLOW
    pool_timeout_s: int = DEFAULT_POOL_TIMEOUT_S
    pool_recycle_s: int = DEFAULT_POOL_RECYCLE_S
    echo_sql: bool = False
    auto_migrate: bool = DEFAULT_AUTO_MIGRATE
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # noqa: D401 — dataclass override
        """Redact the master key and DB password from the default repr.

        The frozen-dataclass default repr printed `master_key=b'...'` and
        the raw URL (which contains the DB password) — keeping that
        secret out of crash logs / interactive output costs nothing.
        """
        return (
            f"KanonStorageConfig(database_url={_redact_url_for_repr(self.database_url)!r}, "
            f"master_key=<redacted {len(self.master_key)} bytes>, "
            f"pool_size={self.pool_size}, pool_overflow={self.pool_overflow}, "
            f"pool_timeout_s={self.pool_timeout_s}, "
            f"pool_recycle_s={self.pool_recycle_s}, "
            f"echo_sql={self.echo_sql}, auto_migrate={self.auto_migrate})"
        )

    @property
    def dialect(self) -> str:
        """Return 'postgresql' or 'sqlite' from the URL.

        Resolves the driver via `make_url`, so ambiguous schemes like
        `postgresql+psycopg://` (sync driver — incompatible with our
        AsyncEngine) get rejected at config time instead of failing
        deep inside `create_async_engine`.
        """
        from sqlalchemy.engine.url import make_url

        try:
            parsed = make_url(self.database_url)
        except Exception as err:
            raise ValueError(
                f"Unparseable database URL: {self.database_url!r}: {err}"
            ) from err

        driver = parsed.drivername
        if driver in ("postgresql+asyncpg",):
            return "postgresql"
        if driver in ("sqlite+aiosqlite",):
            return "sqlite"
        raise ValueError(
            f"Unsupported database driver: {driver!r}. "
            "Supported drivers: postgresql+asyncpg, sqlite+aiosqlite."
        )

    @classmethod
    def from_settings(cls, settings: BaseSettings) -> "KanonStorageConfig":
        """Build a config from the live ACA-Py settings.

        Falls back to env vars (ACAPY_KS_* or KANON_STORAGE_*) and finally
        to documented defaults. Raises ConfigError if a required value is
        missing in production-like contexts.
        """
        # --plugin-config file.yml lands NESTED under
        # settings["plugin_config"]["kanon_storage"][<key>]; the flat dotted
        # form only matches --plugin-config-value. Read both, then env.
        plugin_config = settings.get_value("plugin_config") or {}
        _nested_raw = (
            plugin_config.get("kanon_storage") or {}
            if isinstance(plugin_config, Mapping)
            else {}
        )

        def _expanded(key: str):
            """Nested value, unless it still carries unexpanded ${VAR}
            template placeholders (plugin-config.yml is an envsubst
            template; a missing env leaves the literal behind)."""
            v = _nested_raw.get(key)
            if isinstance(v, str) and "${" in v:
                return None
            return v

        nested = {k: _expanded(k) for k in _nested_raw}

        url = (
            settings.get_value(SETTING_DATABASE_URL)
            or nested.get("database_url")
            or os.environ.get("KANON_STORAGE_DATABASE_URL")
            or os.environ.get("ACAPY_KS_DATABASE_URL")
            or DEFAULT_SQLITE_URL
        )

        raw_key = (
            settings.get_value(SETTING_MASTER_KEY)
            or nested.get("master_key")
            or os.environ.get("KANON_STORAGE_MASTER_KEY")
            or os.environ.get("ACAPY_KS_MASTER_KEY")
        )
        if not raw_key:
            if url == DEFAULT_SQLITE_URL:
                # In-memory dev: derive a non-secret deterministic key so
                # tests/CLI smoke don't need any env config.
                raw_key = b"kanon-storage-dev-key-DO-NOT-USE"
            else:
                raise ConfigError(
                    f"Setting {SETTING_MASTER_KEY!r} (or env KANON_STORAGE_MASTER_KEY) "
                    "is required for non-default database URLs."
                )
        if isinstance(raw_key, str):
            raw_key = raw_key.encode("utf-8")
        # Storage layer uses AES-256 (see Keystore.__init__), so require a
        # full-strength 32-byte key. Silently padding a too-short key with
        # NULs or truncating a too-long one downgrades the at-rest
        # encryption strength without any operator-visible signal.
        if len(raw_key) != 32:
            raise ConfigError(
                f"Setting {SETTING_MASTER_KEY!r} must be exactly 32 bytes "
                f"(got {len(raw_key)}). Use 32 random bytes for AES-256, or "
                "derive via HKDF/scrypt from a passphrase."
            )
        master_key = raw_key

        # `... or DEFAULT_*` swallows operator-set `0` (e.g. pool_size: 0 to
        # force NullPool semantics). Use explicit-None checks so a deliberate
        # zero is honored.
        #
        # Env fallback (KANON_STORAGE_<KEY> / ACAPY_KS_<KEY>) matches
        # database_url/master_key above. Before this, KANON_STORAGE_AUTO_MIGRATE
        # was silently ignored: a fresh deploy configured via env booted with
        # auto_migrate=False, never bootstrapped the schema, and crashed on the
        # first query (UndefinedTableError: kanon_generic_record).
        def _setting_or(key: str, default):
            v = settings.get_value(key)
            if v is None:
                v = nested.get(key.removeprefix(KS_PREFIX))
            if v is None:
                env_suffix = key.removeprefix(KS_PREFIX).upper()
                v = (
                    os.environ.get(f"KANON_STORAGE_{env_suffix}")
                    or os.environ.get(f"ACAPY_KS_{env_suffix}")
                    or None
                )
            return v if v is not None else default

        def _as_bool(v) -> bool:
            # Env vars (and envsubst'd YAML) arrive as strings; bool("false")
            # is True, so parse the usual spellings explicitly.
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return bool(v)

        return cls(
            database_url=url,
            master_key=master_key,
            pool_size=int(_setting_or(SETTING_POOL_SIZE, DEFAULT_POOL_SIZE)),
            pool_overflow=int(_setting_or(SETTING_POOL_OVERFLOW, DEFAULT_POOL_OVERFLOW)),
            pool_timeout_s=int(_setting_or(SETTING_POOL_TIMEOUT, DEFAULT_POOL_TIMEOUT_S)),
            pool_recycle_s=int(_setting_or(SETTING_POOL_RECYCLE, DEFAULT_POOL_RECYCLE_S)),
            echo_sql=_as_bool(_setting_or(SETTING_ECHO_SQL, False)),
            auto_migrate=_as_bool(_setting_or(SETTING_AUTO_MIGRATE, DEFAULT_AUTO_MIGRATE)),
            extra={},
        )


class ConfigError(ValueError):
    """Configuration error for kanon_storage."""

    pass
